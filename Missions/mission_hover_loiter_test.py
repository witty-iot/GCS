"""
ESP32 Guided-takeoff + Loiter-hold hover test — no relay.

Flow:
  arm -> takeoff to HOVER_ALTITUDE (GUIDED) -> set_mode LOITER
       -> hold LOITER for HOVER_SECONDS -> land

Identical flight profile to mission_hover_guided_test.py except the hold
happens in LOITER (ArduPilot's own tuned position-hold controller) instead
of staying in GUIDED. Run both back-to-back over similar conditions:

  - If the GUIDED-only flight holds steady but this one drifts, the mode
    transition itself (or something LOITER-specific) is implicated.
  - If BOTH drift by a similar amount, the cause is common to both modes —
    most likely the underlying EKF position/heading estimate (compass
    interference, vibration, GPS quality) rather than mode-specific control
    logic, since every ArduCopter mode holds position using the same EKF
    estimate.

Usage:
    python mission_hover_loiter_test.py              # upload mission, confirm before start
    python mission_hover_loiter_test.py --start      # upload + immediately start
    python mission_hover_loiter_test.py --stop       # send STOP / LAND command
    python mission_hover_loiter_test.py --status     # print ESP32/Pixhawk status in this terminal
"""

import argparse
import json
import socket
import sys
import time

from flight_logger import FlightLogger, MAV_SEVERITY_NAMES

# ─── ESP32 Connection Settings ───────────────────────────────────────────────
ESP32_IP = "192.168.4.1"
ESP32_PORT = 14551
MAVLINK_PORT = 14552  # dedicated logger broadcast (always-on copy, independent of
                       # Mission Planner's 14550) — see Arduino_esp_code/esp_uploader.txt
TIMEOUT = 3
STATUS_INTERVAL = 1.0
PRESTART_TIMEOUT = 20
MISSION_TIMEOUT = 90
FRESH_TELEMETRY_MS = 5000

logger = None  # set in main(); module-level so helpers below can reach it

# ─── Mission Definition ───────────────────────────────────────────────────────
HOVER_ALTITUDE = 1.5
HOVER_SECONDS = 5

MISSION = {
    "steps": [
        {"action": "arm"},
        {"action": "takeoff", "alt": HOVER_ALTITUDE},
        {"action": "set_mode", "mode": "LOITER"},
        {"action": "wait", "seconds": HOVER_SECONDS},
        {"action": "land"},
    ]
}


# ─── UDP Helpers ─────────────────────────────────────────────────────────────
def send_udp(message, expect_reply=False, timeout=TIMEOUT, quiet=False, retries=1):
    """UDP has no delivery guarantee — a reply (or the request itself) can simply
    get dropped, especially under WiFi load, without that meaning anything failed
    on the ESP32/Pixhawk side. retries resends the same request a few times before
    giving up; safe here because every command this is used for (STATUS, START,
    STOP, mission upload) is a no-op or idempotent to repeat from the ESP32's side."""
    for attempt in range(retries):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            payload = message.encode("utf-8")
            sock.sendto(payload, (ESP32_IP, ESP32_PORT))
            if not quiet:
                print(f"  -> Sent ({len(payload)} bytes)" + (f" [attempt {attempt + 1}/{retries}]" if retries > 1 else ""))
            if expect_reply:
                data, _ = sock.recvfrom(2048)
                return data.decode("utf-8", errors="replace").strip()
        except Exception as exc:
            if expect_reply:
                if attempt + 1 < retries:
                    continue
                return None
            print(f"  X Send failed: {exc}")
            sys.exit(1)
        finally:
            sock.close()
    return None


def request_status(timeout=TIMEOUT):
    reply = send_udp("STATUS", expect_reply=True, timeout=timeout, quiet=True)
    if not reply:
        return None
    try:
        status = json.loads(reply)
    except json.JSONDecodeError:
        return None
    return status if status.get("type") == "status" else None


def step_label(status):
    step = status.get("step", 0)
    total = status.get("total_steps", 0)
    action = status.get("action") or "idle"
    if total:
        return f"{step + 1}/{total} {action}" if step < total else f"{total}/{total} complete"
    return "no mission"


def print_status(status, prefix="STATUS"):
    gps_age = status.get("gps_age_ms")
    hb_age = status.get("heartbeat_age_ms")
    gps_age_text = "n/a" if gps_age is None or gps_age > 60000 else f"{gps_age / 1000:.1f}s"
    hb_age_text = "n/a" if hb_age is None or hb_age > 60000 else f"{hb_age / 1000:.1f}s"
    gps_fix = status.get("gps_fix_type", 0)
    sats = status.get("gps_satellites", 0)
    ekf = status.get("ekf_flags", 0)
    landed = status.get("landed_state", 0)
    ack_cmd = status.get("last_ack_command", 0)
    ack_result = status.get("last_ack_result", 255)
    print(
        f"[{prefix}] step={step_label(status)} running={'YES' if status.get('running') else 'NO'} "
        f"armed={'YES' if status.get('armed') else 'NO'} gps={'OK' if status.get('gps') else 'NO'} "
        f"fix={gps_fix} sats={sats} age={gps_age_text} hb_age={hb_age_text} "
        f"ekf=0x{ekf:04X} landed={landed} alt={status.get('alt', 0):.1f}m "
        f"lat={status.get('lat', 0):.6f} lon={status.get('lon', 0):.6f} "
        f"mode={status.get('mode', 0)} ack={ack_cmd}/{ack_result} "
        f"completed={'YES' if status.get('completed') else 'NO'} "
        f"aborted={'YES' if status.get('aborted') else 'NO'}"
    )
    if status.get("last_abort"):
        print(f"         last_abort={status['last_abort']}")
    if status.get("last_statustext"):
        print(f"         pixhawk_msg(sev={status.get('last_statustext_severity')})={status['last_statustext']}")
    if status.get("last_important_text") and status.get("last_important_age_ms", 999999) <= 60000:
        print(f"         !! PIXHAWK IMPORTANT (sev={status.get('last_important_severity')}) "
              f"= {status['last_important_text']}")


def print_new_statustext():
    """Print any Pixhawk STATUSTEXT messages the FlightLogger captured since last call."""
    if logger is None:
        return
    for severity, text in logger.drain_statustext():
        sev_name = MAV_SEVERITY_NAMES.get(severity, str(severity))
        print(f"         [Pixhawk {sev_name}] {text}")


def telemetry_is_fresh(status, require_gps=True):
    ok = status.get("heartbeat_age_ms", 999999) <= FRESH_TELEMETRY_MS
    if require_gps:
        ok = ok and (
            status.get("gps")
            and status.get("gps_fix_type", 0) >= 3
            and status.get("gps_age_ms", 999999) <= FRESH_TELEMETRY_MS
            and status.get("gps_raw_age_ms", 999999) <= FRESH_TELEMETRY_MS
        )
    return ok


def require_prestart_ready(force=False):
    print("[Preflight] Waiting for fresh ESP/Pixhawk telemetry and GPS lock...")
    if force:
        print("[Preflight] --force set: skipping this script's local GPS-fix wait. Pixhawk's own arming "
              "checks still run and will reject ARM (with a real PreArm reason) if it isn't actually ready.")
    deadline = time.time() + PRESTART_TIMEOUT
    last_status = None
    while time.time() < deadline:
        status = request_status(timeout=1.5)
        if status:
            last_status = status
            print_status(status, "Preflight")
            print_new_statustext()
            if not status.get("mission_stored"):
                print("[ERROR] ESP32 does not report a stored mission.")
                if logger:
                    logger.note("Preflight aborted: ESP32 does not report a stored mission")
                sys.exit(1)
            if status.get("running"):
                print("[ERROR] ESP32 says a mission is already running.")
                if logger:
                    logger.note("Preflight aborted: mission already running")
                sys.exit(1)
            if telemetry_is_fresh(status, require_gps=not force):
                print("[Preflight] Fresh Pixhawk heartbeat and GPS telemetry are live.")
                print("[Preflight] Pixhawk arming checks remain enabled and will decide whether ARM is accepted.")
                if logger:
                    logger.note("Preflight ready" + (" (GPS wait skipped via --force)" if force else ""))
                return
        else:
            print("[Preflight] No UDP status reply from ESP32 yet...")
        time.sleep(STATUS_INTERVAL)

    print("\n[ERROR] Preflight did not become ready.")
    if last_status:
        print_status(last_status, "Last")
        if last_status.get("gps_satellites", 0) == 0:
            print("        Hint: 0 GPS satellites — this is almost always an outdoor-sky-view requirement, "
                  "not a Pixhawk rejection (indoors this will never resolve). Re-run with --force to skip "
                  "this script's local wait and let Pixhawk's own prearm checks run/report instead.")
    else:
        print("        No STATUS reply received. Flash the updated ESP firmware, check WiFi, IP, and UDP port.")
    if logger:
        logger.note("Preflight timed out waiting for fresh telemetry/GPS")
    sys.exit(1)


def monitor_mission():
    print("[3/3] Monitoring mission telemetry. Press Ctrl+C to send STOP/LAND.")
    deadline = time.time() + MISSION_TIMEOUT
    arm_confirmed = False
    consecutive_misses = 0
    try:
        while time.time() < deadline:
            status = request_status(timeout=1.5)
            if not status:
                consecutive_misses += 1
                print(f"[Monitor] No STATUS reply from ESP32 ({consecutive_misses} in a row)")
                if consecutive_misses == 5:
                    print("[Monitor] WARNING: ESP32 has been unreachable for several seconds while the "
                          "vehicle may still be flying. It is not receiving fresh commands or abort/timeout "
                          "supervision right now. Be ready to take over on the RC transmitter.")
                time.sleep(STATUS_INTERVAL)
                continue
            consecutive_misses = 0

            print_status(status, "Monitor")
            print_new_statustext()
            if status.get("armed") and not arm_confirmed:
                arm_confirmed = True
                print("[Monitor] Pixhawk accepted ARM. Arming checks passed at arm time.")
                if logger:
                    logger.note("Pixhawk accepted ARM")
            if status.get("aborted"):
                print("\n[ERROR] Mission aborted by ESP32/Pixhawk path.")
                if logger:
                    esp_reason = status.get("last_abort") or "unknown"
                    pixhawk_reason = status.get("last_important_text")
                    note = f"Mission aborted: {esp_reason}"
                    if pixhawk_reason:
                        note += f" (Pixhawk: {pixhawk_reason})"
                    logger.note(note)
                sys.exit(1)
            if status.get("completed"):
                print("\nMission complete.")
                if logger:
                    logger.note("Mission completed")
                return
            if not status.get("running") and status.get("step", 0) > 0:
                print("\n[WARN] Mission is no longer running, but completion was not reported.")
                if logger:
                    logger.note("Mission stopped running without reporting completion")
                return
            time.sleep(STATUS_INTERVAL)
    except KeyboardInterrupt:
        print("\nCtrl+C received. Sending STOP/LAND...")
        if logger:
            logger.note("Operator pressed Ctrl+C — sending STOP/LAND")
        send_stop()
        sys.exit(130)

    print("\n[ERROR] Mission monitor timed out.")
    if logger:
        logger.note("Mission monitor timed out")
    sys.exit(1)


def send_mission():
    data = json.dumps(MISSION, separators=(",", ":"))
    print(f"\n[1/3] Uploading Guided-takeoff + Loiter-hold mission ({len(MISSION['steps'])} steps)...")
    for i, step in enumerate(MISSION["steps"], start=1):
        extras = {k: v for k, v in step.items() if k != "action"}
        suffix = "  " + "  ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        print(f"       {i}. {step['action']}{suffix}")
    print()
    reply = send_udp(data, expect_reply=True)
    if reply != "OK MISSION_STORED":
        print("[ERROR] ESP32 did not confirm mission storage.")
        print(f"        Reply: {reply or 'no UDP reply'}")
        if logger:
            logger.note(f"Mission upload failed: {reply or 'no UDP reply'}")
        sys.exit(1)
    print("  <- ESP32 confirmed mission storage")
    if logger:
        logger.note("Mission uploaded and stored on ESP32")
    time.sleep(0.3)


def send_start():
    print("[2/3] Sending START...")
    # A dropped UDP ack does NOT mean the vehicle didn't start -- ESP32 sends
    # "OK START" before the mission even begins executing, so if this reply is
    # lost the vehicle can still be armed and flying. Retry first (harmless:
    # ESP32 replies "ERROR already_running" to a retry if the first one landed),
    # and if still inconclusive, ask STATUS directly before ever assuming failure.
    reply = send_udp("START", expect_reply=True, retries=3, timeout=2)
    if reply == "OK START":
        pass
    elif reply == "ERROR already_running":
        print("[WARN] ESP32 reports mission already running — an earlier START "
              "attempt landed but its acknowledgment was lost. Continuing to monitor.")
        if logger:
            logger.note("START ack was lost, but ESP32 confirms mission already running")
    else:
        status = request_status(timeout=2)
        if status and status.get("running"):
            print("[WARN] No START acknowledgment was received, but STATUS confirms the "
                  "mission IS running on the ESP32/Pixhawk. Continuing to monitor -- "
                  "do not assume the vehicle is idle just because this ack was lost.")
            if logger:
                logger.note("START ack lost, but STATUS confirms mission running")
        else:
            print("[ERROR] ESP32 refused START.")
            print(f"        Reply: {reply or 'no UDP reply'}")
            if status:
                print(f"        STATUS confirms not running (running={status.get('running')}, "
                      f"armed={status.get('armed')}) — safe to treat as a real failure.")
            else:
                print("        STATUS check also got no reply — verify manually before assuming "
                      "the vehicle is idle.")
            if logger:
                logger.note(f"START refused: {reply or 'no UDP reply'}")
            sys.exit(1)
    print("      Mission started on ESP32")
    print(f"      Expect: arm -> takeoff to {HOVER_ALTITUDE}m -> switch to LOITER -> "
          f"hold {HOVER_SECONDS}s -> land\n")
    if logger:
        logger.note("START sent and acknowledged")


def send_stop():
    print("Sending STOP (LAND will be commanded on the drone)...")
    # Idempotent on the ESP32 (abortMission() + LAND fires again each time), so
    # retrying a lost ack here is always safe and never re-arms/re-starts anything.
    reply = send_udp("STOP", expect_reply=True, retries=3, timeout=2)
    if reply:
        print(f"ESP32 reply: {reply}")
        print("STOP acknowledged by ESP32\n")
        if logger:
            logger.note(f"STOP acknowledged: {reply}")
    else:
        print("[WARNING] No reply from ESP32 — it may not have received/processed STOP.")
        print("          Do not assume the vehicle is landing. Use manual RC override if available.\n")
        if logger:
            logger.note("STOP sent but no ESP32 reply received")


def send_status():
    print("Requesting STATUS from ESP32...")
    status = request_status()
    if not status:
        print("[ERROR] No UDP STATUS reply. Flash the updated ESP firmware or check WiFi/IP/port.")
        sys.exit(1)
    print_status(status)


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    global ESP32_IP, ESP32_PORT, logger

    parser = argparse.ArgumentParser(description="ESP32 Guided-takeoff + Loiter-hold hover test (no relay)")
    parser.add_argument("--start", action="store_true", help="Upload and immediately start mission")
    parser.add_argument("--stop", action="store_true", help="Send STOP command (triggers LAND)")
    parser.add_argument("--status", action="store_true", help="Request status from ESP32")
    parser.add_argument("--ip", default=ESP32_IP, help="ESP32 IP (default: 192.168.4.1)")
    parser.add_argument("--port", default=ESP32_PORT, type=int, help="Mission UDP port (default: 14551)")
    parser.add_argument("--mav-port", default=MAVLINK_PORT, type=int,
                         help="MAVLink UDP port to capture for logging (default: 14552, the dedicated "
                              "always-broadcast logger port; pass 14550 for the old behavior, which "
                              "conflicts with a connected Mission Planner/QGC)")
    parser.add_argument("--force", action="store_true",
                         help="Skip this script's local GPS-fix wait (e.g. for indoor testing). "
                              "Pixhawk's own arming checks still run and will reject/report via STATUSTEXT.")
    args = parser.parse_args()

    ESP32_IP = args.ip
    ESP32_PORT = args.port

    print("=" * 60)
    print("  ESP32 Guided-Takeoff + Loiter-Hold Test (no relay)")
    print(f"  Target: {ESP32_IP}:{ESP32_PORT}")
    print(f"  Takeoff to {HOVER_ALTITUDE}m (GUIDED), switch to LOITER, hold {HOVER_SECONDS}s, land")
    print("=" * 60)

    logger = FlightLogger(args.mav_port, run_name="hover_loiter", mission=MISSION)
    logger.start()

    try:
        if args.stop:
            logger.note("Operator requested STOP")
            send_stop()
            return

        if args.status:
            send_status()
            return

        send_mission()
        require_prestart_ready(force=args.force)

        if args.start:
            send_start()
        else:
            confirm = input("  Type 'yes' to start the mission, anything else to cancel: ").strip().lower()
            if confirm == "yes":
                logger.note("Operator confirmed START at prompt")
                send_start()
            else:
                print("\n  Mission uploaded but NOT started.")
                print("  To start later run:  python mission_hover_loiter_test.py --start\n")
                logger.note("Operator cancelled before START")
                return

        monitor_mission()
        print("  To abort at any time run:  python mission_hover_loiter_test.py --stop\n")
    finally:
        path = logger.stop()
        print(f"\n[Log] Detailed flight log written: {path}")


if __name__ == "__main__":
    main()
