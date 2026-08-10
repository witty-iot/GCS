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
COMMAND_ACK, attitude/RC-stick/position-target snapshots, and periodic
GPS/battery/EKF/vibration snapshots -- plus, since instability (overshoot,
drift) is what this project keeps needing to root-cause, it actively flags
the specific signals known to correlate with that: vibration spikes, EKF
falling back to a degraded/no-position-confidence state, and (via
POSITION_TARGET_GLOBAL_INT vs GLOBAL_POSITION_INT) whether the vehicle
actually overshot the altitude the autopilot itself was targeting, or was
just following a target that was never 1.5m in the first place -- e.g. from
RC stick override, which RC_CHANNELS logging also makes visible after the
fact. A FLIGHT SUMMARY block flagging all of this is appended at the end of
every log, so a run can be triaged without re-deriving all of this by hand.

At the end of a run the timeline is written to logs/<run_name>_<timestamp>.log
so a flight can be diagnosed from that file without opening Mission Planner.

This is intentionally passive/best-effort: if the MAVLink UDP port can't be
opened (blocked port, pymavlink missing, etc.) the logger still records the
script's own milestone notes and writes a (smaller) log file rather than
failing the mission.
"""

import datetime
import math
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

# ESTIMATOR_STATUS_FLAGS (EKF_STATUS_REPORT.flags) bit -> name.
_EKF_BITS = [
    (1, "attitude"), (2, "vel_horiz"), (4, "vel_vert"),
    (8, "pos_horiz_rel"), (16, "pos_horiz_abs"), (32, "pos_vert_abs"),
    (64, "pos_vert_agl"), (128, "CONST_POS_MODE"),
    (256, "pred_pos_horiz_rel"), (512, "pred_pos_horiz_abs"),
    (1024, "UNINITIALIZED"),
]

# Vibration (m/s/s) on any axis above this is flagged -- ~15-16 was measured
# during the 2026-08-05 flight that overshot its takeoff altitude to 9m.
VIBE_ALERT_THRESHOLD = 15.0

# Message types worth recording every time they're seen (low rate / event-driven).
_ALWAYS_LOG = {"STATUSTEXT", "COMMAND_ACK", "EXTENDED_SYS_STATE"}
# Message types that stream at a few Hz — throttled so the log stays readable.
# (This only throttles the routine snapshot line -- alert/summary tracking for
# EKF and vibration runs on every message regardless, see _on_EKF_STATUS_REPORT
# and _on_VIBRATION.)
_THROTTLE_SECONDS = {
    "GPS_RAW_INT": 2.0,
    "GLOBAL_POSITION_INT": 1.0,
    "SYS_STATUS": 2.0,
    "EKF_STATUS_REPORT": 2.0,
    "VIBRATION": 1.0,
    "VFR_HUD": 2.0,
    "BATTERY_STATUS": 5.0,
    "ATTITUDE": 1.0,
    "RC_CHANNELS": 1.0,
    "POSITION_TARGET_GLOBAL_INT": 1.0,
}


def _decode_ekf(flags):
    """Returns (set_of_flag_names_present, list_of_plain_english_concerns)."""
    have = {name for bit, name in _EKF_BITS if flags & bit}
    concerns = []
    if "UNINITIALIZED" in have:
        concerns.append("EKF uninitialized")
    if "CONST_POS_MODE" in have:
        concerns.append("fallen back to constant-position mode (not trusting horizontal position)")
    if "pos_horiz_abs" not in have:
        concerns.append("no absolute horizontal position confidence")
    if "pos_horiz_rel" not in have:
        concerns.append("no relative horizontal position confidence")
    if "vel_vert" not in have:
        concerns.append("no vertical velocity confidence")
    return have, concerns


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

        self._ekf_healthy = None
        self._vibe_high = {"x": False, "y": False, "z": False}
        self._last_relalt_m = None

        self._target_alt_m = self._extract_target_alt(mission)

        self._summary = {
            "max_vibe": {"x": (0.0, None), "y": (0.0, None), "z": (0.0, None)},
            "vibe_alert_count": 0,
            "ekf_degraded_events": 0,
            "ekf_degraded_seconds": 0.0,
            "ekf_worst_flags": None,
            "ekf_worst_concerns": [],
            "max_relalt_m": (None, None),
            "max_throttle_pct": (None, None),
            "max_rc_throttle_pwm": (None, None),
            "min_battery_v": (None, None),
            "max_current_a": (None, None),
            "critical_messages": [],
        }
        self._ekf_degraded_since = None

    @staticmethod
    def _extract_target_alt(mission):
        try:
            for step in mission.get("steps", []):
                if step.get("action") == "takeoff" and "alt" in step:
                    return float(step["alt"])
        except Exception:
            pass
        return None

    # ─── Public API ──────────────────────────────────────────────────────
    def start(self):
        self._log("=" * 70)
        self._log(f"Flight log: {self.run_name}")
        self._log(f"Started: {datetime.datetime.now().isoformat(timespec='seconds')}")
        if self.mission:
            self._log(f"Mission: {self.mission}")
        if self._target_alt_m is not None:
            self._log(f"Takeoff target altitude: {self._target_alt_m:.1f}m")
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

        self._close_ekf_degraded_span()
        self._log_summary()

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

    def _track_max(self, key, value, unit=""):
        cur_value, _ = self._summary[key]
        if cur_value is None or value > cur_value:
            self._summary[key] = (value, self._elapsed())

    def _track_min(self, key, value):
        cur_value, _ = self._summary[key]
        if cur_value is None or value < cur_value:
            self._summary[key] = (value, self._elapsed())

    def _close_ekf_degraded_span(self):
        if self._ekf_degraded_since is not None:
            self._summary["ekf_degraded_seconds"] += self._elapsed() - self._ekf_degraded_since
            self._ekf_degraded_since = None

    def _handle(self, msg):
        t = msg.get_type()
        handler = getattr(self, f"_on_{t}", None)
        if handler:
            handler(msg)

    # ─── Per-message-type handlers ─────────────────────────────────────
    def _on_STATUSTEXT(self, msg):
        sev = MAV_SEVERITY_NAMES.get(msg.severity, str(msg.severity))
        text = msg.text.rstrip("\x00")
        self._log(f"PIXHAWK MSG [{sev}] {text}")
        with self._lock:
            self._new_statustext.append((msg.severity, text))
        if msg.severity <= 3:  # EMERGENCY/ALERT/CRITICAL/ERROR
            self._summary["critical_messages"].append((self._elapsed(), sev, text))

    def _on_HEARTBEAT(self, msg):
        if getattr(msg, "type", 0) == 6:
            return  # MAV_TYPE_GCS -- ignore Mission Planner/QGC/our own heartbeat
        armed = bool(msg.base_mode & MAV_MODE_FLAG_SAFETY_ARMED)
        mode = CUSTOM_MODE_NAMES.get(msg.custom_mode, str(msg.custom_mode))
        if armed != self._armed:
            self._log(f"{'ARMED' if armed else 'DISARMED'} (mode={mode})")
            self._armed = armed
        if mode != self._mode:
            self._log(f"Mode change -> {mode}")
            self._mode = mode

    def _on_COMMAND_ACK(self, msg):
        result = MAV_RESULT_NAMES.get(msg.result, str(msg.result))
        self._log(f"COMMAND_ACK command={msg.command} result={result}")

    def _on_SYS_STATUS(self, msg):
        volts = msg.voltage_battery / 1000
        amps = msg.current_battery / 100
        self._track_min("min_battery_v", volts)
        self._track_max("max_current_a", amps)
        if self._throttled("SYS_STATUS"):
            return
        self._log(
            f"SYS_STATUS battery={volts:.2f}V current={amps:.1f}A "
            f"remaining={msg.battery_remaining}% load={msg.load / 10:.1f}%"
        )

    def _on_GPS_RAW_INT(self, msg):
        if self._throttled("GPS_RAW_INT"):
            return
        self._log(
            f"GPS fix={msg.fix_type} sats={msg.satellites_visible} "
            f"hdop={msg.eph / 100:.2f} lat={msg.lat / 1e7:.7f} lon={msg.lon / 1e7:.7f} "
            f"alt={msg.alt / 1000:.2f}m"
        )

    def _on_GLOBAL_POSITION_INT(self, msg):
        relalt = msg.relative_alt / 1000
        self._last_relalt_m = relalt
        self._track_max("max_relalt_m", relalt)
        if self._throttled("GLOBAL_POSITION_INT"):
            return
        self._log(
            f"POSITION lat={msg.lat / 1e7:.7f} lon={msg.lon / 1e7:.7f} "
            f"relalt={relalt:.2f}m hdg={msg.hdg / 100:.1f}deg"
        )

    def _on_POSITION_TARGET_GLOBAL_INT(self, msg):
        """What the autopilot is actually TARGETING right now. Comparing this to
        GLOBAL_POSITION_INT's actual relalt is how you tell "the vehicle overshot
        a 1.5m target" apart from "the target itself wasn't 1.5m" (e.g. RC stick
        override, or a mission step commanding something else)."""
        target_alt = msg.alt  # relative-alt frames report this in meters already
        delta = ""
        if self._last_relalt_m is not None:
            delta = f"  (actual relalt={self._last_relalt_m:.2f}m, error={self._last_relalt_m - target_alt:+.2f}m)"
        if self._throttled("POSITION_TARGET_GLOBAL_INT"):
            return
        self._log(f"POSITION_TARGET alt={target_alt:.2f}m{delta}")

    def _on_ATTITUDE(self, msg):
        if self._throttled("ATTITUDE"):
            return
        self._log(
            f"ATTITUDE roll={math.degrees(msg.roll):.1f}deg pitch={math.degrees(msg.pitch):.1f}deg "
            f"yaw={math.degrees(msg.yaw):.1f}deg "
            f"rollspeed={math.degrees(msg.rollspeed):.1f}deg/s "
            f"pitchspeed={math.degrees(msg.pitchspeed):.1f}deg/s "
            f"yawspeed={math.degrees(msg.yawspeed):.1f}deg/s"
        )

    def _on_RC_CHANNELS(self, msg):
        """Logged mainly so a future review can tell autonomous behavior apart from
        the pilot's own stick input (chan3 = throttle by RC convention, matching
        the firmware's own "Throttle (RC3) is not neutral" PreArm wording)."""
        throttle_pwm = getattr(msg, "chan3_raw", None)
        if throttle_pwm:
            self._track_max("max_rc_throttle_pwm", throttle_pwm)
        if self._throttled("RC_CHANNELS"):
            return
        self._log(
            f"RC_STICKS roll={getattr(msg, 'chan1_raw', '?')} pitch={getattr(msg, 'chan2_raw', '?')} "
            f"throttle={throttle_pwm} yaw={getattr(msg, 'chan4_raw', '?')}"
        )

    def _on_EKF_STATUS_REPORT(self, msg):
        have, concerns = _decode_ekf(msg.flags)
        healthy_now = not concerns

        # Transition-based alert -- fires immediately, not subject to throttling,
        # so a brief degraded spell between snapshot samples is never missed.
        if healthy_now != self._ekf_healthy:
            if self._ekf_healthy is not None:  # skip the very first reading
                if not healthy_now:
                    self._log(f"[ALERT] EKF became degraded: {'; '.join(concerns)}")
                    self._summary["ekf_degraded_events"] += 1
                    self._ekf_degraded_since = self._elapsed()
                else:
                    self._log("[ALERT] EKF recovered to healthy")
                    self._close_ekf_degraded_span()
            self._ekf_healthy = healthy_now

        if concerns:
            worst = self._summary["ekf_worst_flags"]
            if worst is None or len(concerns) >= len(self._summary["ekf_worst_concerns"]):
                self._summary["ekf_worst_flags"] = msg.flags
                self._summary["ekf_worst_concerns"] = concerns

        if self._throttled("EKF_STATUS_REPORT"):
            return
        line = f"EKF flags=0x{msg.flags:04X}"
        if concerns:
            line += " -- CONCERN: " + "; ".join(concerns)
        self._log(line)

    def _on_VIBRATION(self, msg):
        axes = {"x": msg.vibration_x, "y": msg.vibration_y, "z": msg.vibration_z}
        for axis, value in axes.items():
            cur_value, _ = self._summary["max_vibe"][axis]
            if value > cur_value:
                self._summary["max_vibe"][axis] = (value, self._elapsed())

            was_high = self._vibe_high[axis]
            is_high = value > VIBE_ALERT_THRESHOLD
            if is_high and not was_high:
                self._log(f"[ALERT] High vibration on {axis.upper()}-axis: {value:.2f} m/s/s "
                           f"(threshold {VIBE_ALERT_THRESHOLD})")
                self._summary["vibe_alert_count"] += 1
            self._vibe_high[axis] = is_high

        if self._throttled("VIBRATION"):
            return
        self._log(
            f"VIBRATION x={msg.vibration_x:.2f} y={msg.vibration_y:.2f} z={msg.vibration_z:.2f} "
            f"clipping=({msg.clipping_0},{msg.clipping_1},{msg.clipping_2})"
        )

    def _on_VFR_HUD(self, msg):
        self._track_max("max_throttle_pct", msg.throttle)
        if self._throttled("VFR_HUD"):
            return
        self._log(
            f"VFR airspeed={msg.airspeed:.1f}m/s groundspeed={msg.groundspeed:.1f}m/s "
            f"climb={msg.climb:.1f}m/s throttle={msg.throttle}%"
        )

    def _on_BATTERY_STATUS(self, msg):
        if self._throttled("BATTERY_STATUS"):
            return
        self._log(f"BATTERY remaining={msg.battery_remaining}%")

    def _on_EXTENDED_SYS_STATE(self, msg):
        self._log(f"landed_state={msg.landed_state}")

    # ─── Summary ─────────────────────────────────────────────────────────
    def _log_summary(self):
        s = self._summary
        self._log("-" * 70)
        self._log("FLIGHT SUMMARY (auto-generated -- flags things known to correlate")
        self._log("with the overshoot/drift instability this project has been chasing)")
        self._log("-" * 70)

        any_flag = False

        max_vibe = s["max_vibe"]
        if any(t is not None for _val, t in max_vibe.values()):
            vibe_line = "  ".join(f"{axis}={val:.2f}" for axis, (val, _t) in max_vibe.items())
            worst_axis, (worst_val, worst_t) = max(max_vibe.items(), key=lambda kv: kv[1][0])
            vibe_flag = " [FLAG: exceeded alert threshold]" if worst_val > VIBE_ALERT_THRESHOLD else ""
            self._log(f"Max vibration (m/s/s): {vibe_line}  "
                       f"(worst: {worst_axis}={worst_val:.2f} at +{worst_t:.1f}s){vibe_flag}")
            if vibe_flag:
                any_flag = True

        if s["max_relalt_m"][0] is not None:
            actual, at = s["max_relalt_m"]
            if self._target_alt_m:
                ratio = actual / self._target_alt_m if self._target_alt_m else 0
                overshoot_flag = " [FLAG: overshot takeoff target]" if ratio > 1.5 else ""
                self._log(f"Max altitude: {actual:.2f}m at +{at:.1f}s vs takeoff target "
                           f"{self._target_alt_m:.1f}m ({ratio:.1f}x){overshoot_flag}")
                if overshoot_flag:
                    any_flag = True
            else:
                self._log(f"Max altitude: {actual:.2f}m at +{at:.1f}s (no takeoff target in mission)")

        if s["ekf_degraded_events"] or s["ekf_worst_flags"] is not None:
            worst = s["ekf_worst_flags"]
            worst_str = f"0x{worst:04X}" if worst is not None else "n/a"
            self._log(f"EKF degraded {s['ekf_degraded_events']} time(s), "
                      f"{s['ekf_degraded_seconds']:.1f}s total. Worst flags={worst_str} "
                      f"-- {'; '.join(s['ekf_worst_concerns']) or 'n/a'} [FLAG]")
            any_flag = True

        if s["max_rc_throttle_pwm"][0] is not None:
            pwm, at = s["max_rc_throttle_pwm"]
            note = ""
            if pwm > 1700:
                note = " [FLAG: high RC throttle stick seen -- check for manual override during this run]"
                any_flag = True
            self._log(f"Max RC throttle stick: {pwm}us at +{at:.1f}s{note}")

        if s["max_throttle_pct"][0] is not None:
            pct, at = s["max_throttle_pct"]
            note = " [FLAG: near-max autopilot throttle output -- check thrust margin]" if pct >= 90 else ""
            self._log(f"Max autopilot throttle output: {pct}% at +{at:.1f}s{note}")
            if note:
                any_flag = True

        if s["min_battery_v"][0] is not None:
            v, at = s["min_battery_v"]
            a, at_a = s["max_current_a"] if s["max_current_a"][0] is not None else (None, None)
            extra = f", max current {a:.1f}A at +{at_a:.1f}s" if a is not None else ""
            self._log(f"Min battery voltage: {v:.2f}V at +{at:.1f}s{extra}")

        if s["critical_messages"]:
            self._log(f"Critical/error Pixhawk messages ({len(s['critical_messages'])}):")
            for t, sev, text in s["critical_messages"]:
                self._log(f"  +{t:.1f}s [{sev}] {text}")
            any_flag = True

        if not any_flag:
            self._log("No flagged concerns from the signals this logger tracks.")
