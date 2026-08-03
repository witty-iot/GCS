"""
Passive MAVLink telemetry logger shared by the mission_*.py scripts.

The ESP32 firmware forwards every raw MAVLink byte it receives from the
Pixhawk out over UDP 14550 (the port Mission Planner/QGroundControl connect
to) AND, as an always-broadcast duplicate, over a dedicated UDP 14552 meant
only for this logger — see LOGGER_UDP_PORT / forwardMavlinkToUdp() in
Arduino_esp_code/esp_uploader.txt. The dedicated port exists because Windows
lets Mission Planner's .NET UDP socket bind 14550 exclusively, which blocks
any other process (including this one) from also receiving on it whenever
Mission Planner is connected -- 14552 is never touched by Mission Planner, so
this logger can always attach to it independently of whether Mission Planner
is open.

This module listens on that port in the background, decodes it with
pymavlink, and records a full chronological timeline of what ArduCopter
actually said during a run: every STATUSTEXT (the same messages Mission
Planner's Messages tab shows), every arm/disarm and mode change, every
COMMAND_ACK, plus periodic GPS/battery/EKF/vibration snapshots.

At the end of a run the timeline is written to logs/<run_name>_<timestamp>.log
so a flight can be diagnosed from that file without opening Mission Planner.

This is intentionally passive/best-effort: if the MAVLink UDP port can't be
opened (blocked port, pymavlink missing, etc.) the logger still records the
script's own milestone notes and writes a (smaller) log file rather than
failing the mission.
"""

import datetime
import os
import threading
import time

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

MAV_SEVERITY_NAMES = {
    0: "EMERGENCY", 1: "ALERT", 2: "CRITICAL", 3: "ERROR",
    4: "WARNING", 5: "NOTICE", 6: "INFO", 7: "DEBUG",
}

MAV_RESULT_NAMES = {
    0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED",
    3: "UNSUPPORTED", 4: "FAILED", 5: "IN_PROGRESS", 6: "CANCELLED",
}

MAV_MODE_FLAG_SAFETY_ARMED = 0x80

# ArduCopter custom_mode values (Copter.h enum control_mode_t)
CUSTOM_MODE_NAMES = {
    0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
    5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND", 11: "DRIFT",
    13: "SPORT", 14: "FLIP", 15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE",
    18: "THROW", 19: "AVOID_ADSB", 20: "GUIDED_NOGPS", 21: "SMART_RTL",
    22: "FLOWHOLD", 23: "FOLLOW", 24: "ZIGZAG", 25: "SYSTEMID",
    26: "AUTOROTATE", 27: "AUTO_RTL",
}

# Message types worth recording every time they're seen (low rate / event-driven).
_ALWAYS_LOG = {"STATUSTEXT", "COMMAND_ACK", "EXTENDED_SYS_STATE"}
# Message types that stream at a few Hz — throttled so the log stays readable.
_THROTTLE_SECONDS = {
    "GPS_RAW_INT": 2.0,
    "GLOBAL_POSITION_INT": 2.0,
    "SYS_STATUS": 2.0,
    "EKF_STATUS_REPORT": 2.0,
    "VIBRATION": 2.0,
    "VFR_HUD": 2.0,
    "BATTERY_STATUS": 5.0,
}


class FlightLogger:
    """Background MAVLink sniffer + timeline writer for a single mission run."""

    def __init__(self, mav_port, run_name, mission=None):
        self.mav_port = mav_port
        self.run_name = run_name
        self.mission = mission

        self._lock = threading.Lock()
        self._events = []
        self._new_statustext = []
        self._start_time = time.time()
        self._armed = None
        self._mode = None
        self._last_logged_at = {}
        self._stop = threading.Event()
        self._conn = None
        self._thread = None

    # ─── Public API ──────────────────────────────────────────────────────
    def start(self):
        self._log("=" * 70)
        self._log(f"Flight log: {self.run_name}")
        self._log(f"Started: {datetime.datetime.now().isoformat(timespec='seconds')}")
        if self.mission:
            self._log(f"Mission: {self.mission}")
        self._log("=" * 70)

        try:
            from pymavlink import mavutil
            self._conn = mavutil.mavlink_connection(
                f"udpin:0.0.0.0:{self.mav_port}", dialect="ardupilotmega", autoreconnect=True
            )
        except Exception as exc:
            self._log(f"[logger] Could not open MAVLink capture on UDP {self.mav_port}: {exc}")
            self._log("[logger] Continuing with script-level notes only (no Pixhawk message capture).")
            self._conn = None
            return

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def note(self, line):
        """Record a script-level milestone (upload, START/STOP, abort reason, ...)."""
        self._log(f"[script] {line}")

    def drain_statustext(self):
        """Return and clear any STATUSTEXT messages received since the last call."""
        with self._lock:
            items, self._new_statustext = self._new_statustext, []
        return items

    def stop(self, result_summary=None):
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

        self._log("=" * 70)
        if result_summary:
            self._log(f"Result: {result_summary}")
        self._log(f"Ended: {datetime.datetime.now().isoformat(timespec='seconds')} "
                   f"(duration {self._elapsed():.1f}s)")
        self._log("=" * 70)
        return self._write()

    # ─── Internal ────────────────────────────────────────────────────────
    def _elapsed(self):
        return time.time() - self._start_time

    def _log(self, line):
        with self._lock:
            self._events.append(f"[+{self._elapsed():7.2f}s] {line}")

    def _write(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(LOG_DIR, f"{self.run_name}_{stamp}.log")
        with self._lock:
            lines = list(self._events)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _run(self):
        while not self._stop.is_set():
            try:
                msg = self._conn.recv_match(blocking=True, timeout=0.5)
            except Exception:
                continue
            if msg is None:
                continue
            try:
                self._handle(msg)
            except Exception as exc:
                self._log(f"[logger] error decoding {msg.get_type()}: {exc}")

    def _throttled(self, msg_type):
        interval = _THROTTLE_SECONDS.get(msg_type)
        if interval is None:
            return False
        now = time.time()
        last = self._last_logged_at.get(msg_type, 0)
        if now - last < interval:
            return True
        self._last_logged_at[msg_type] = now
        return False

    def _handle(self, msg):
        t = msg.get_type()
        if t not in _ALWAYS_LOG and self._throttled(t):
            return

        if t == "STATUSTEXT":
            sev = MAV_SEVERITY_NAMES.get(msg.severity, str(msg.severity))
            text = msg.text.rstrip("\x00")
            self._log(f"PIXHAWK MSG [{sev}] {text}")
            with self._lock:
                self._new_statustext.append((msg.severity, text))

        elif t == "HEARTBEAT" and getattr(msg, "type", 0) != 6:
            # type 6 == MAV_TYPE_GCS -- ignore heartbeats from Mission Planner/QGC/us,
            # only track the vehicle's own heartbeat (autopilot component).
            armed = bool(msg.base_mode & MAV_MODE_FLAG_SAFETY_ARMED)
            mode = CUSTOM_MODE_NAMES.get(msg.custom_mode, str(msg.custom_mode))
            if armed != self._armed:
                self._log(f"{'ARMED' if armed else 'DISARMED'} (mode={mode})")
                self._armed = armed
            if mode != self._mode:
                self._log(f"Mode change -> {mode}")
                self._mode = mode

        elif t == "COMMAND_ACK":
            result = MAV_RESULT_NAMES.get(msg.result, str(msg.result))
            self._log(f"COMMAND_ACK command={msg.command} result={result}")

        elif t == "SYS_STATUS":
            self._log(
                f"SYS_STATUS battery={msg.voltage_battery / 1000:.2f}V "
                f"current={msg.current_battery / 100:.1f}A "
                f"remaining={msg.battery_remaining}% load={msg.load / 10:.1f}%"
            )

        elif t == "GPS_RAW_INT":
            self._log(
                f"GPS fix={msg.fix_type} sats={msg.satellites_visible} "
                f"hdop={msg.eph / 100:.2f} lat={msg.lat / 1e7:.7f} lon={msg.lon / 1e7:.7f} "
                f"alt={msg.alt / 1000:.2f}m"
            )

        elif t == "GLOBAL_POSITION_INT":
            self._log(
                f"POSITION lat={msg.lat / 1e7:.7f} lon={msg.lon / 1e7:.7f} "
                f"relalt={msg.relative_alt / 1000:.2f}m hdg={msg.hdg / 100:.1f}deg"
            )

        elif t == "EKF_STATUS_REPORT":
            self._log(f"EKF flags=0x{msg.flags:04X}")

        elif t == "VIBRATION":
            self._log(
                f"VIBRATION x={msg.vibration_x:.2f} y={msg.vibration_y:.2f} z={msg.vibration_z:.2f} "
                f"clipping=({msg.clipping_0},{msg.clipping_1},{msg.clipping_2})"
            )

        elif t == "VFR_HUD":
            self._log(
                f"VFR airspeed={msg.airspeed:.1f}m/s groundspeed={msg.groundspeed:.1f}m/s "
                f"climb={msg.climb:.1f}m/s throttle={msg.throttle}%"
            )

        elif t == "BATTERY_STATUS":
            self._log(f"BATTERY remaining={msg.battery_remaining}%")

        elif t == "EXTENDED_SYS_STATE":
            self._log(f"landed_state={msg.landed_state}")
