"""
Minimal mode-switch test script for ESP32 mission manager.
This does NOT arm the drone and does NOT send takeoff/relay/land commands.
It only uploads a mission that changes flight modes in sequence for testing.

Usage:
    python mode_test_uploader.py --start
"""

import socket
import json
import argparse
import time
import sys

ESP32_IP = "192.168.4.1"
ESP32_PORT = 14550
TIMEOUT = 3

MISSION = {
    "steps": [
        {"action": "set_mode", "mode": "GUIDED"},
        {"action": "wait", "seconds": 2},
        {"action": "set_mode", "mode": "LOITER"},
        {"action": "wait", "seconds": 2},
        {"action": "set_mode", "mode": "GUIDED"},
        {"action": "wait", "seconds": 2},
        {"action": "set_mode", "mode": "LOITER"},
        {"action": "wait", "seconds": 2},
    ]
}


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
    print(f"\n[1/2] Uploading mode test mission ({len(MISSION['steps'])} steps)...")
    for i, step in enumerate(MISSION["steps"]):
        extras = {k: v for k, v in step.items() if k != "action"}
        extra_str = "  " + "  ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        print(f"       {i+1}. {step['action']}{extra_str}")
    print()
    send_udp(data)
    time.sleep(0.3)


def send_start():
    print("[2/2] Sending START...")
    send_udp("START")
    print("      Mode test mission started on ESP32")
    print("      This mission only changes modes and waits.\n")
    time.sleep(35)


def main():
    global ESP32_IP, ESP32_PORT

    parser = argparse.ArgumentParser(description="Minimal ESP32 mode-switch test uploader")
    parser.add_argument("--start", action="store_true", help="Upload and immediately start mission")
    parser.add_argument("--ip", default=ESP32_IP, help="ESP32 IP (default: 192.168.4.1)")
    parser.add_argument("--port", default=ESP32_PORT, type=int, help="UDP port (default: 14550)")
    args = parser.parse_args()

    ESP32_IP = args.ip
    ESP32_PORT = args.port

    print("=" * 55)
    print("  ESP32 Mode Switch Test Uploader")
    print(f"  Target: {ESP32_IP}:{ESP32_PORT}")
    print("=" * 55)

    send_mission()

    if args.start:
        send_start()
    else:
        confirm = input("  Type 'yes' to start the mission, anything else to cancel: ").strip().lower()
        if confirm == "yes":
            send_start()
        else:
            print("\n  Mission uploaded but NOT started.")
            print("  To start later run:  python mode_test_uploader.py --start\n")
            return

    print("[3/3] Done.")


if __name__ == "__main__":
    main()
