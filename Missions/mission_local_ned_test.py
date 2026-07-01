"""
ESP32 local-NED position target test.

Flow:
  arm -> takeoff -> SET_POSITION_TARGET_LOCAL_NED position -> land

Local NED uses X=north, Y=east, Z=down, relative to ArduPilot's EKF
origin. So Z=-2.5 means 2.5 m above that local origin. Keep X/Y tiny
for the first test.
"""

import argparse
import json
import socket
import sys
import time

ESP32_IP = "192.168.4.1"
ESP32_PORT = 14551
TIMEOUT = 3
STATUS_INTERVAL = 1.0
PRESTART_TIMEOUT = 20
MISSION_TIMEOUT = 120
FRESH_TELEMETRY_MS = 5000

TAKEOFF_ALTITUDE = 2.5
LOCAL_X_NORTH_M = 5.0
LOCAL_Y_EAST_M = 0.0
LOCAL_Z_DOWN_M = -2.5
HOLD_SECONDS = 8

MISSION = {
    "steps": [
        {"action": "arm"},
        {"action": "takeoff", "alt": TAKEOFF_ALTITUDE},
        {
            "action": "local_ned",
            "x": LOCAL_X_NORTH_M,
            "y": LOCAL_Y_EAST_M,
            "z": LOCAL_Z_DOWN_M,
            "seconds": HOLD_SECONDS,
        },
        {"action": "land"},
    ]
}


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


def print_status(status, prefix="STATUS"):
    print(
        f"[{prefix}] step={status.get('step', 0) + 1}/{status.get('total_steps', 0)} "
        f"{status.get('action') or 'idle'} running={'YES' if status.get('running') else 'NO'} "
        f"armed={'YES' if status.get('armed') else 'NO'} gps={'OK' if status.get('gps') else 'NO'} "
        f"alt={status.get('alt', 0):.1f}m lat={status.get('lat', 0):.6f} "
        f"lon={status.get('lon', 0):.6f} mode={status.get('mode', 0)} "
        f"completed={'YES' if status.get('completed') else 'NO'} aborted={'YES' if status.get('aborted') else 'NO'}"
    )
    if status.get("last_abort"):
        print(f"         last_abort={status['last_abort']}")


def telemetry_is_fresh(status):
    return (
        status.get("gps")
        and status.get("gps_age_ms", 999999) <= FRESH_TELEMETRY_MS
        and status.get("heartbeat_age_ms", 999999) <= FRESH_TELEMETRY_MS
    )


def require_prestart_ready():
    print("[Preflight] Waiting for fresh telemetry...")
    deadline = time.time() + PRESTART_TIMEOUT
    while time.time() < deadline:
        status = request_status(timeout=1.5)
        if status:
            print_status(status, "Preflight")
            if telemetry_is_fresh(status) and not status.get("running"):
                return
        else:
            print("[Preflight] No UDP status reply from ESP32 yet...")
        time.sleep(STATUS_INTERVAL)
    print("[ERROR] Preflight did not become ready.")
    sys.exit(1)


def upload_start_monitor(start_immediately):
    data = json.dumps(MISSION, separators=(",", ":"))
    print(f"\n[1/3] Uploading local-NED test ({len(MISSION['steps'])} steps)...")
    for i, step in enumerate(MISSION["steps"], start=1):
        extras = {k: v for k, v in step.items() if k != "action"}
        suffix = "  " + "  ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        print(f"       {i}. {step['action']}{suffix}")
    reply = send_udp(data, expect_reply=True)
    if reply != "OK MISSION_STORED":
        print(f"[ERROR] Mission upload failed: {reply or 'no UDP reply'}")
        sys.exit(1)

    require_prestart_ready()
    if not start_immediately:
        confirm = input("  Type 'yes' to start the mission, anything else to cancel: ").strip().lower()
        if confirm != "yes":
            print("Mission uploaded but NOT started.")
            return

    print("[2/3] Sending START...")
    reply = send_udp("START", expect_reply=True)
    if reply != "OK START":
        print(f"[ERROR] START failed: {reply or 'no UDP reply'}")
        sys.exit(1)

    print("[3/3] Monitoring. Press Ctrl+C to STOP/LAND.")
    deadline = time.time() + MISSION_TIMEOUT
    try:
        while time.time() < deadline:
            status = request_status(timeout=1.5)
            if status:
                print_status(status, "Monitor")
                if status.get("aborted"):
                    sys.exit(1)
                if status.get("completed"):
                    print("Mission complete.")
                    return
            else:
                print("[Monitor] No STATUS reply from ESP32")
            time.sleep(STATUS_INTERVAL)
    except KeyboardInterrupt:
        send_udp("STOP", expect_reply=True)
        sys.exit(130)

    print("[ERROR] Mission monitor timed out.")
    sys.exit(1)


def main():
    global ESP32_IP, ESP32_PORT
    parser = argparse.ArgumentParser(description="ESP32 local-NED test mission")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--ip", default=ESP32_IP)
    parser.add_argument("--port", default=ESP32_PORT, type=int)
    args = parser.parse_args()
    ESP32_IP = args.ip
    ESP32_PORT = args.port

    if args.stop:
        print(send_udp("STOP", expect_reply=True) or "STOP sent")
        return
    if args.status:
        status = request_status()
        if not status:
            print("[ERROR] No STATUS reply.")
            sys.exit(1)
        print_status(status)
        return

    print("=" * 58)
    print("  ESP32 Local-NED Position Target Test")
    print(f"  X north={LOCAL_X_NORTH_M}m  Y east={LOCAL_Y_EAST_M}m  Z down={LOCAL_Z_DOWN_M}m")
    print("=" * 58)
    upload_start_monitor(args.start)


if __name__ == "__main__":
    main()
