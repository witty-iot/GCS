"""
ESP32 Drone Mission Uploader
Usage:
    python mission_uploader.py              # upload mission, confirm before start
    python mission_uploader.py --start      # upload + immediately start
    python mission_uploader.py --stop       # send STOP / RTL command
    python mission_uploader.py --status     # request status print on ESP32 serial
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
MISSION = {
    "steps": [
        { "action": "takeoff", "alt": 5 },
        { "action": "wait",    "seconds": 20 },
        { "action": "relay_on" },
        { "action": "wait",    "seconds": 20 },
        { "action": "relay_off" },
        { "action": "wait",    "seconds": 20 },
        { "action": "rtl" }
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
    print("      Waiting for mission to complete (~150s)...")
    print("      Watch ESP32 serial monitor for step logs.\n")
    time.sleep(150)  # Wait for takeoff + relay cycle + RTL + buffer

def send_stop():
    print("Sending STOP (RTL will be commanded on the drone)...")
    send_udp("STOP")
    print("STOP sent\n")

def send_status():
    print("Sending STATUS (check ESP32 serial monitor for output)...")
    send_udp("STATUS")

# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    global ESP32_IP, ESP32_PORT

    parser = argparse.ArgumentParser(description="ESP32 Drone Mission Uploader")
    parser.add_argument("--start",  action="store_true", help="Upload and immediately start mission")
    parser.add_argument("--stop",   action="store_true", help="Send STOP command (triggers RTL)")
    parser.add_argument("--status", action="store_true", help="Request status from ESP32")
    parser.add_argument("--ip",     default=ESP32_IP,    help="ESP32 IP (default: 192.168.4.1)")
    parser.add_argument("--port",   default=ESP32_PORT,  type=int, help="UDP port (default: 14550)")
    args = parser.parse_args()

    ESP32_IP   = args.ip
    ESP32_PORT = args.port

    print("=" * 55)
    print("  ESP32 Drone Mission Uploader")
    print(f"  Target: {ESP32_IP}:{ESP32_PORT}")
    print("=" * 55)

    if args.stop:
        send_stop()
        return

    if args.status:
        send_status()
        return

    send_mission()

    if args.start:
        send_start()
    else:
        confirm = input("  Type 'yes' to start the mission, anything else to cancel: ").strip().lower()
        if confirm == "yes":
            send_start()
        else:
            print("\n  Mission uploaded but NOT started.")
            print("  To start later run:  python mission_uploader.py --start\n")
            return

    print("[3/3] Done.")
    print("  Mission execution complete.")
    print("  To abort at any time run:  python mission_uploader.py --stop\n")

if __name__ == "__main__":
    main()