"""
ESP32 Drone Agriculture Spray Mission Uploader
Autonomous spray mission: Takeoff → Fly to Point A → Slow spray run to Point B → RTL

Usage:
    python mission_uploader2.py              # upload mission, confirm before start
    python mission_uploader2.py --start      # upload + immediately start
    python mission_uploader2.py --stop       # send STOP / LAND command
    python mission_uploader2.py --status     # print ESP32/Pixhawk status in this terminal

IMPORTANT - Speed Control:
    For slow spray speed (e.g., 1 m/s), set these drone parameters BEFORE running mission:
        - WPNAV_SPEED = 100 (1 m/s in cm/s)
        - WPNAV_SPEED_MAX = 100
    For normal return speed, the RTL will use default setting.
"""

import socket
import json
import argparse
import time
import sys

# ─── ESP32 Connection Settings ───────────────────────────────────────────────
ESP32_IP   = "192.168.4.1"
ESP32_PORT = 14551
TIMEOUT    = 3
STATUS_INTERVAL = 1.0
PRESTART_TIMEOUT = 20
MISSION_TIMEOUT = 300
FRESH_TELEMETRY_MS = 5000

# ─── Mission Definition ───────────────────────────────────────────────────────
# FILL IN COORDINATES FOR POINT A AND POINT B
POINT_A_LAT = 0.000000  # ← FILL IN YOUR LATITUDE
POINT_A_LON = 0.000000  # ← FILL IN YOUR LONGITUDE
POINT_B_LAT = 0.000100  # ← FILL IN YOUR LATITUDE (usually slightly different from A)
POINT_B_LON = 0.000100  # ← FILL IN YOUR LONGITUDE

SPRAY_ALTITUDE = 2.5    # Meters (adjust as needed for your plants)

MISSION = {
    "steps": [
        # ─── PHASE 1: Takeoff ───────────────────────────────────────────
        {"action": "arm"},
        {"action": "takeoff", "alt": SPRAY_ALTITUDE},
        
        # ─── PHASE 2: Navigate to Point A ───────────────────────────────
        # Fly to Point A at spray altitude
        {"action": "fly_to", "lat": POINT_A_LAT, "lon": POINT_A_LON, "alt": SPRAY_ALTITUDE},
        
        # ─── PHASE 3: Slow Spray Run from A to B ───────────────────────
        # Enable relay (start spraying)
        {"action": "relay_on"},
        
        # Fly slowly from Point A to Point B
        # Speed will be controlled by drone's WPNAV_SPEED parameter (set to ~100 cm/s = 1 m/s)
        {"action": "fly_to", "lat": POINT_B_LAT, "lon": POINT_B_LON, "alt": SPRAY_ALTITUDE},
        
        # ─── PHASE 4: End Spray and Return ──────────────────────────────
        # Disable relay (stop spraying)
        {"action": "relay_off"},
        
        # Return to launch at normal speed
        {"action": "rtl"}
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
    except Exception as e:
        if expect_reply:
            return None
        print(f"  X Send failed: {e}")
        sys.exit(1)
    finally:
        sock.close()
    return None

def parse_status(reply):
    if not reply:
        return None
    try:
        status = json.loads(reply)
    except json.JSONDecodeError:
        return None
    return status if status.get("type") == "status" else None

def request_status(timeout=TIMEOUT):
    return parse_status(send_udp("STATUS", expect_reply=True, timeout=timeout, quiet=True))

def step_label(status):
    step = status.get("step", 0)
    total = status.get("total_steps", 0)
    action = status.get("action") or "idle"
    if total:
        return f"{step + 1}/{total} {action}" if step < total else f"{total}/{total} complete"
    return "no mission"

def format_status(status):
    gps = "OK" if status.get("gps") else "NO"
    armed = "YES" if status.get("armed") else "NO"
    running = "YES" if status.get("running") else "NO"
    completed = "YES" if status.get("completed") else "NO"
    aborted = "YES" if status.get("aborted") else "NO"
    gps_age = status.get("gps_age_ms")
    hb_age = status.get("heartbeat_age_ms")
    gps_age_text = "n/a" if gps_age is None or gps_age > 60000 else f"{gps_age / 1000:.1f}s"
    hb_age_text = "n/a" if hb_age is None or hb_age > 60000 else f"{hb_age / 1000:.1f}s"
    return (
        f"step={step_label(status)}  running={running}  armed={armed}  "
        f"gps={gps} age={gps_age_text}  hb_age={hb_age_text}  alt={status.get('alt', 0):.1f}m  "
        f"lat={status.get('lat', 0):.6f} lon={status.get('lon', 0):.6f}  "
        f"mode={status.get('mode', 0)}  completed={completed} aborted={aborted}"
    )

def telemetry_is_fresh(status):
    gps_age = status.get("gps_age_ms", 999999)
    heartbeat_age = status.get("heartbeat_age_ms", 999999)
    return (
        status.get("gps")
        and gps_age <= FRESH_TELEMETRY_MS
        and heartbeat_age <= FRESH_TELEMETRY_MS
    )

def print_status(status, prefix="STATUS"):
    print(f"[{prefix}] {format_status(status)}")
    if status.get("last_abort"):
        print(f"         last_abort={status['last_abort']}")

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
    print(f"\n[1/3] Uploading mission ({len(MISSION['steps'])} steps)...")
    for i, step in enumerate(MISSION["steps"]):
        extras = {k: v for k, v in step.items() if k != "action"}
        extra_str = "  " + "  ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        print(f"       {i+1}. {step['action']}{extra_str}")
    print()
    reply = send_udp(data, expect_reply=True)
    if reply != "OK MISSION_STORED":
        print("[ERROR] ESP32 did not confirm mission storage.")
        if reply:
            print(f"        Reply: {reply}")
        else:
            print("        No UDP reply. Flash the updated ESP firmware or check the connection.")
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
    print("      Terminal telemetry monitor is active.")
    print("      The drone will:")
    print(f"        1. Arm and takeoff to {SPRAY_ALTITUDE}m")
    print("        2. Navigate to Point A")
    print("        3. Enable relay and spray slowly to Point B")
    print("        4. Disable relay and RTL")
    print()

def send_stop():
    print("Sending STOP (LAND will be commanded on the drone)...")
    reply = send_udp("STOP", expect_reply=True)
    if reply:
        print(f"ESP32 reply: {reply}")
    print("STOP sent - drone should LAND now\n")

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

    parser = argparse.ArgumentParser(description="ESP32 Drone Agriculture Spray Mission Uploader")
    parser.add_argument("--start",  action="store_true", help="Upload and immediately start mission")
    parser.add_argument("--stop",   action="store_true", help="Send STOP command (triggers LAND)")
    parser.add_argument("--status", action="store_true", help="Request status from ESP32")
    parser.add_argument("--ip",     default=ESP32_IP,    help="ESP32 IP (default: 192.168.4.1)")
    parser.add_argument("--port",   default=ESP32_PORT,  type=int, help="Mission UDP port (default: 14551)")
    args = parser.parse_args()

    ESP32_IP   = args.ip
    ESP32_PORT = args.port

    print("=" * 60)
    print("  ESP32 Drone Agriculture Spray Mission")
    print(f"  Target: {ESP32_IP}:{ESP32_PORT}")
    print("=" * 60)

    if args.stop:
        send_stop()
        return

    if args.status:
        send_status()
        return

    # Check if coordinates are filled in
    if POINT_A_LAT == 0.0 or POINT_A_LON == 0.0 or POINT_B_LAT == 0.0 or POINT_B_LON == 0.0:
        print("\n[ERROR] Coordinates not filled in!")
        print("        Edit this file and fill in:")
        print("        - POINT_A_LAT, POINT_A_LON (where to start spraying)")
        print("        - POINT_B_LAT, POINT_B_LON (where to end spraying)")
        print()
        sys.exit(1)

    # Display mission info
    print("\nMISSION COORDINATES:")
    print(f"  Point A (start spray):  {POINT_A_LAT:.6f}, {POINT_A_LON:.6f}")
    print(f"  Point B (end spray):    {POINT_B_LAT:.6f}, {POINT_B_LON:.6f}")
    print(f"  Altitude:               {SPRAY_ALTITUDE}m")
    print()

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
            print("  To start later run:  python mission_uploader2.py --start\n")
            return

    monitor_mission()
    print("  To land/abort at any time run:  python mission_uploader2.py --stop\n")

if __name__ == "__main__":
    main()
