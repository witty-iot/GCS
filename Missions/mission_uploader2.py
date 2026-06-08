"""
ESP32 Drone Agriculture Spray Mission Uploader
Autonomous spray mission: Takeoff → Fly to Point A → Slow spray run to Point B → RTL

Usage:
    python mission_uploader2.py              # upload mission, confirm before start
    python mission_uploader2.py --start      # upload + immediately start
    python mission_uploader2.py --stop       # send STOP / RTL command
    python mission_uploader2.py --status     # request status print on ESP32 serial

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
ESP32_PORT = 14550
TIMEOUT    = 3

# ─── Mission Definition ───────────────────────────────────────────────────────
# FILL IN COORDINATES FOR POINT A AND POINT B
POINT_A_LAT = 0.000000  # ← FILL IN YOUR LATITUDE
POINT_A_LON = 0.000000  # ← FILL IN YOUR LONGITUDE
POINT_B_LAT = 0.000100  # ← FILL IN YOUR LATITUDE (usually slightly different from A)
POINT_B_LON = 0.000100  # ← FILL IN YOUR LONGITUDE

SPRAY_ALTITUDE = 5.0    # Meters (adjust as needed for your plants)

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
def send_udp(message):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)
    try:
        payload = message.encode("utf-8")
        sock.sendto(payload, (ESP32_IP, ESP32_PORT))
        print(f"  -> Sent ({len(payload)} bytes)")
    except Exception as e:
        print(f"  X Send failed: {e}")
        sys.exit(1)
    finally:
        sock.close()

def send_mission():
    data = json.dumps(MISSION, separators=(",", ":"))
    print(f"\n[1/3] Uploading mission ({len(MISSION['steps'])} steps)...")
    for i, step in enumerate(MISSION["steps"]):
        extras = {k: v for k, v in step.items() if k != "action"}
        extra_str = "  " + "  ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        print(f"       {i+1}. {step['action']}{extra_str}")
    print()
    send_udp(data)
    time.sleep(0.3)

def send_start():
    print("[2/3] Sending START...")
    send_udp("START")
    print("      Mission started on ESP32")
    print("      Watch ESP32 serial monitor for step logs.")
    print("      The drone will:")
    print("        1. Arm and takeoff to 5m")
    print("        2. Navigate to Point A")
    print("        3. Enable relay and spray slowly to Point B")
    print("        4. Disable relay and RTL")
    print()

def send_stop():
    print("Sending STOP (RTL will be commanded on the drone)...")
    send_udp("STOP")
    print("STOP sent - drone should RTL now\n")

def send_status():
    print("Sending STATUS (check ESP32 serial monitor for output)...")
    send_udp("STATUS")

# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    global ESP32_IP, ESP32_PORT

    parser = argparse.ArgumentParser(description="ESP32 Drone Agriculture Spray Mission Uploader")
    parser.add_argument("--start",  action="store_true", help="Upload and immediately start mission")
    parser.add_argument("--stop",   action="store_true", help="Send STOP command (triggers RTL)")
    parser.add_argument("--status", action="store_true", help="Request status from ESP32")
    parser.add_argument("--ip",     default=ESP32_IP,    help="ESP32 IP (default: 192.168.4.1)")
    parser.add_argument("--port",   default=ESP32_PORT,  type=int, help="UDP port (default: 14550)")
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

    print("[3/3] Done.")
    print("  Mission execution started on ESP32.")
    print("  To abort at any time run:  python mission_uploader2.py --stop\n")

if __name__ == "__main__":
    main()
