"""
ESP32 relay-only test.

Uploads and starts a mission that switches the relay on for 5 seconds,
then switches it off. No arming, mode changes, takeoff, or disarm.
"""

import json
import socket
import sys
import time


ESP32_IP = "192.168.4.1"
ESP32_PORT = 14550
TIMEOUT = 3

MISSION = {
    "steps": [
        {"action": "relay_on"},
        {"action": "wait", "seconds": 5},
        {"action": "relay_off"},
    ]
}


def send_udp(message):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)
    try:
        payload = message.encode("utf-8")
        sock.sendto(payload, (ESP32_IP, ESP32_PORT))
        print(f"  -> Sent ({len(payload)} bytes)")
    except Exception as exc:
        print(f"  X Send failed: {exc}")
        sys.exit(1)
    finally:
        sock.close()


print("=" * 45)
print("  ESP32 Relay Test")
print(f"  Target: {ESP32_IP}:{ESP32_PORT}")
print("=" * 45)

print("\n[1/3] Uploading relay-only mission...")
send_udp(json.dumps(MISSION, separators=(",", ":")))
time.sleep(0.3)

print("[2/3] Sending START...")
send_udp("START")

print("[3/3] Relay should be ON for 5 seconds, then OFF.")
print("      Watch ESP32 serial monitor for relay step logs.")
