"""
ESP32 pure-Guided hover test — no mode switching, no relay.

Flow:
  arm -> takeoff to HOVER_ALTITUDE -> hold in GUIDED for HOVER_SECONDS -> land

The hold step is a plain "wait": no position/velocity target is resent
during it, so the vehicle just keeps whatever GUIDED position target
takeoff left it on. Per ArduPilot's docs, GUIDED position targets don't
need to be resent to be held.

This script intentionally never leaves GUIDED mode. Run it back-to-back
with mission_hover_loiter_test.py (same flight, but switches to LOITER for
the hold) to check whether in-flight instability tracks the flight mode
itself, or something common to both flights (EKF quality, vibration, EMI).

Usage:
    python mission_hover_guided_test.py              # upload mission, confirm before start
    python mission_hover_guided_test.py --start      # upload + immediately start
    python mission_hover_guided_test.py --stop       # send STOP / LAND command
    python mission_hover_guided_test.py --status     # print ESP32/Pixhawk status in this terminal
"""

import argparse
import json
import socket
import sys
import time

# ─── ESP32 Connection Settings ───────────────────────────────────────────────
ESP32_IP = "192.168.4.1"
ESP32_PORT = 14551
TIMEOUT = 3
STATUS_INTERVAL = 1.0
PRESTART_TIMEOUT = 20
MISSION_TIMEOUT = 90
FRESH_TELEMETRY_MS = 5000

# ─── Mission Definition ───────────────────────────────────────────────────────
HOVER_ALTITUDE = 1.5
HOVER_SECONDS = 5

MISSION = {
    "steps": [
        {"action": "arm"},
        {"action": "takeoff", "alt": HOVER_ALTITUDE},
        {"action": "wait", "seconds": HOVER_SECONDS},
        {"action": "land"},
    ]
}


# ─── UDP Helpers ─────────────────────────────────────────────────────────────
def send_udp(message, expect_reply=False, timeout=TIMEOUT, quiet=False):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        payload = message.encode("utf-8")
        sock.sendto(payload, (ESP32_IP, ESP32_PORT))
        if not quiet:
            print(f"  -> Sent ({len(payload)} bytes)")
        if expect_reply:
            data, _ = sock.recvfrom(2048)
            return data.decode("utf-8", errors="replace").strip()
    except Exception as exc:
        if expect_reply:
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


def telemetry_is_fresh(status):
    return (
        status.get("gps")
        and status.get("gps_fix_type", 0) >= 3
        and status.get("gps_age_ms", 999999) <= FRESH_TELEMETRY_MS
        and status.get("gps_raw_age_ms", 999999) <= FRESH_TELEMETRY_MS
        and status.get("heartbeat_age_ms", 999999) <= FRESH_TELEMETRY_MS
    )


def require_prestart_ready():
    print("[Preflight] Waiting for fresh ESP/Pixhawk telemetry and GPS lock...")
    deadline = time.time() + PRESTART_TIMEOUT
    last_status = None
    while time.time() < deadline:
        status = request_status(timeout=1.5)
        if status:
            last_status = status
            print_status(status, "Preflight")
            if not status.get("mission_stored"):
                print("[ERROR] ESP32 does not report a stored mission.")
                sys.exit(1)
            if status.get("running"):
                print("[ERROR] ESP32 says a mission is already running.")
                sys.exit(1)
            if telemetry_is_fresh(status):
                print("[Preflight] Fresh Pixhawk heartbeat and GPS telemetry are live.")
                print("[Preflight] Pixhawk arming checks remain enabled and will decide whether ARM is accepted.")
                return
        else:
            print("[Preflight] No UDP status reply from ESP32 yet...")
        time.sleep(STATUS_INTERVAL)

    print("\n[ERROR] Preflight did not become ready.")
    if last_status:
        print_status(last_status, "Last")
    else:
        print("        No STATUS reply received. Flash the updated ESP firmware, check WiFi, IP, and UDP port.")
    sys.exit(1)


def monitor_mission():
    print("[3/3] Monitoring mission telemetry. Press Ctrl+C to send STOP/LAND.")
    deadline = time.time() + MISSION_TIMEOUT
    arm_confirmed = False
    try:
        while time.time() < deadline:
            status = request_status(timeout=1.5)
            if not status:
                print("[Monitor] No STATUS reply from ESP32")
                time.sleep(STATUS_INTERVAL)
                continue

            print_status(status, "Monitor")
            if status.get("armed") and not arm_confirmed:
                arm_confirmed = True
                print("[Monitor] Pixhawk accepted ARM. Arming checks passed at arm time.")
            if status.get("aborted"):
                print("\n[ERROR] Mission aborted by ESP32/Pixhawk path.")
                sys.exit(1)
            if status.get("completed"):
                print("\nMission complete.")
                return
            if not status.get("running") and status.get("step", 0) > 0:
                print("\n[WARN] Mission is no longer running, but completion was not reported.")
                return
            time.sleep(STATUS_INTERVAL)
    except KeyboardInterrupt:
        print("\nCtrl+C received. Sending STOP/LAND...")
        send_stop()
        sys.exit(130)

    print("\n[ERROR] Mission monitor timed out.")
    sys.exit(1)


def send_mission():
    data = json.dumps(MISSION, separators=(",", ":"))
    print(f"\n[1/3] Uploading pure-Guided hover mission ({len(MISSION['steps'])} steps)...")
    for i, step in enumerate(MISSION["steps"], start=1):
        extras = {k: v for k, v in step.items() if k != "action"}
        suffix = "  " + "  ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        print(f"       {i}. {step['action']}{suffix}")
    print()
    reply = send_udp(data, expect_reply=True)
    if reply != "OK MISSION_STORED":
        print("[ERROR] ESP32 did not confirm mission storage.")
        print(f"        Reply: {reply or 'no UDP reply'}")
        sys.exit(1)
    print("  <- ESP32 confirmed mission storage")
    time.sleep(0.3)


def send_start():
    print("[2/3] Sending START...")
    reply = send_udp("START", expect_reply=True)
    if reply != "OK START":
        print("[ERROR] ESP32 refused START.")
        print(f"        Reply: {reply or 'no UDP reply'}")
        sys.exit(1)
    print("      Mission started on ESP32")
    print(f"      Expect: arm -> takeoff to {HOVER_ALTITUDE}m -> hold {HOVER_SECONDS}s in GUIDED -> land\n")


def send_stop():
    print("Sending STOP (LAND will be commanded on the drone)...")
    reply = send_udp("STOP", expect_reply=True)
    if reply:
        print(f"ESP32 reply: {reply}")
    print("STOP sent\n")


def send_status():
    print("Requesting STATUS from ESP32...")
    status = request_status()
    if not status:
        print("[ERROR] No UDP STATUS reply. Flash the updated ESP firmware or check WiFi/IP/port.")
        sys.exit(1)
    print_status(status)


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    global ESP32_IP, ESP32_PORT

    parser = argparse.ArgumentParser(description="ESP32 pure-Guided hover test (no mode switch, no relay)")
    parser.add_argument("--start", action="store_true", help="Upload and immediately start mission")
    parser.add_argument("--stop", action="store_true", help="Send STOP command (triggers LAND)")
    parser.add_argument("--status", action="store_true", help="Request status from ESP32")
    parser.add_argument("--ip", default=ESP32_IP, help="ESP32 IP (default: 192.168.4.1)")
    parser.add_argument("--port", default=ESP32_PORT, type=int, help="Mission UDP port (default: 14551)")
    args = parser.parse_args()

    ESP32_IP = args.ip
    ESP32_PORT = args.port

    print("=" * 60)
    print("  ESP32 Pure-Guided Hover Test (no LOITER, no relay)")
    print(f"  Target: {ESP32_IP}:{ESP32_PORT}")
    print(f"  Takeoff to {HOVER_ALTITUDE}m, hold {HOVER_SECONDS}s in GUIDED, land")
    print("=" * 60)

    if args.stop:
        send_stop()
        return

    if args.status:
        send_status()
        return

    send_mission()
    require_prestart_ready()

    if args.start:
        send_start()
    else:
        confirm = input("  Type 'yes' to start the mission, anything else to cancel: ").strip().lower()
        if confirm == "yes":
            send_start()
        else:
            print("\n  Mission uploaded but NOT started.")
            print("  To start later run:  python mission_hover_guided_test.py --start\n")
            return

    monitor_mission()
    print("  To abort at any time run:  python mission_hover_guided_test.py --stop\n")


if __name__ == "__main__":
    main()
