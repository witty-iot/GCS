"""
ESP32 Guided reposition test mission.

Flow:
  arm -> takeoff -> MAV_CMD_DO_REPOSITION -> wait briefly -> land
"""

import argparse
import json
import math
import socket
import sys
import time

ESP32_IP = "192.168.4.1"
ESP32_PORT = 14551
TIMEOUT = 3
STATUS_INTERVAL = 1.0
PRESTART_TIMEOUT = 20
MISSION_TIMEOUT = 180
FRESH_TELEMETRY_MS = 5000
MAX_START_DISTANCE_M = 25.0

# Put one nearby field coordinate here.
TARGET_LAT = 28.583908
TARGET_LON = 77.352030
TARGET_ALTITUDE = 2.5
HOLD_SECONDS = 3

MISSION = {
    "steps": [
        {"action": "arm"},
        {"action": "takeoff", "alt": TARGET_ALTITUDE},
        {"action": "reposition", "lat": TARGET_LAT, "lon": TARGET_LON, "alt": TARGET_ALTITUDE},
        {"action": "wait", "seconds": HOLD_SECONDS},
        {"action": "land"},
    ]
}


def distance_m(lat1, lon1, lat2, lon2):
    radius_m = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


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
    print("[Preflight] Waiting for ESP/Pixhawk telemetry and GPS lock...")
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
                return status
        else:
            print("[Preflight] No UDP status reply from ESP32 yet...")
        time.sleep(STATUS_INTERVAL)

    print("\n[ERROR] Preflight did not become ready.")
    if last_status:
        print_status(last_status, "Last")
    sys.exit(1)


def validate_target_distance(status, max_distance_m, force_distance=False):
    target_distance = distance_m(status["lat"], status["lon"], TARGET_LAT, TARGET_LON)
    print(f"[Preflight] Current GPS to target: {target_distance:.1f}m")
    if target_distance > max_distance_m and not force_distance:
        print("\n[ERROR] Target is farther than the configured test limit.")
        print(f"        Current: {status['lat']:.6f}, {status['lon']:.6f}")
        print(f"        Target:  {TARGET_LAT:.6f}, {TARGET_LON:.6f}")
        print(f"        Distance: {target_distance:.1f}m; limit: {max_distance_m:.1f}m")
        print("        Use a closer test point or rerun with --force-distance only in a clear field.")
        sys.exit(1)


def send_mission():
    data = json.dumps(MISSION, separators=(",", ":"))
    print(f"\n[1/3] Uploading Guided reposition test mission ({len(MISSION['steps'])} steps)...")
    for i, step in enumerate(MISSION["steps"], start=1):
        extras = {k: v for k, v in step.items() if k != "action"}
        suffix = "  " + "  ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        print(f"       {i}. {step['action']}{suffix}")
    reply = send_udp(data, expect_reply=True)
    if reply != "OK MISSION_STORED":
        print("[ERROR] ESP32 did not confirm mission storage.")
        print(f"        Reply: {reply or 'no UDP reply'}")
        sys.exit(1)
    print("  <- ESP32 confirmed mission storage")


def send_start():
    print("[2/3] Sending START...")
    reply = send_udp("START", expect_reply=True)
    if reply != "OK START":
        print("[ERROR] ESP32 refused START.")
        print(f"        Reply: {reply or 'no UDP reply'}")
        sys.exit(1)
    print("      Mission started on ESP32")


def send_stop():
    print("Sending STOP (LAND will be commanded on the drone)...")
    reply = send_udp("STOP", expect_reply=True)
    if reply:
        print(f"ESP32 reply: {reply}")
    print("STOP sent\n")


def send_status():
    status = request_status()
    if not status:
        print("[ERROR] No UDP STATUS reply.")
        sys.exit(1)
    print_status(status)


def monitor_mission():
    print("[3/3] Monitoring mission telemetry. Press Ctrl+C to send STOP/LAND.")
    deadline = time.time() + MISSION_TIMEOUT
    try:
        while time.time() < deadline:
            status = request_status(timeout=1.5)
            if not status:
                print("[Monitor] No STATUS reply from ESP32")
                time.sleep(STATUS_INTERVAL)
                continue
            print_status(status, "Monitor")
            if status.get("aborted"):
                print("\n[ERROR] Mission aborted.")
                sys.exit(1)
            if status.get("completed"):
                print("\nMission complete.")
                return
            time.sleep(STATUS_INTERVAL)
    except KeyboardInterrupt:
        print("\nCtrl+C received.")
        send_stop()
        sys.exit(130)

    print("\n[ERROR] Mission monitor timed out.")
    sys.exit(1)


def main():
    global ESP32_IP, ESP32_PORT

    parser = argparse.ArgumentParser(description="ESP32 Guided reposition test uploader")
    parser.add_argument("--start", action="store_true", help="Upload and immediately start mission")
    parser.add_argument("--stop", action="store_true", help="Send STOP command")
    parser.add_argument("--status", action="store_true", help="Request status from ESP32")
    parser.add_argument("--ip", default=ESP32_IP, help="ESP32 IP")
    parser.add_argument("--port", default=ESP32_PORT, type=int, help="Mission UDP port")
    parser.add_argument("--max-start-distance", default=MAX_START_DISTANCE_M, type=float)
    parser.add_argument("--force-distance", action="store_true")
    args = parser.parse_args()

    ESP32_IP = args.ip
    ESP32_PORT = args.port

    print("=" * 60)
    print("  ESP32 Guided Reposition Test Mission")
    print(f"  Target ESP32: {ESP32_IP}:{ESP32_PORT}")
    print(f"  Waypoint: {TARGET_LAT:.6f}, {TARGET_LON:.6f} at {TARGET_ALTITUDE:.1f}m")
    print("=" * 60)

    if args.stop:
        send_stop()
        return
    if args.status:
        send_status()
        return

    send_mission()
    prestart_status = require_prestart_ready()
    validate_target_distance(prestart_status, args.max_start_distance, args.force_distance)

    if args.start:
        send_start()
    else:
        confirm = input("  Type 'yes' to start the mission, anything else to cancel: ").strip().lower()
        if confirm != "yes":
            print("\n  Mission uploaded but NOT started.")
            return
        send_start()

    monitor_mission()


if __name__ == "__main__":
    main()
