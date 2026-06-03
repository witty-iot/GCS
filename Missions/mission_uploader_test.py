"""
ESP32 Test Script — Arm, Relay ON 5s, Relay OFF, Disarm
"""

import socket
import json
import time
import sys

ESP32_IP   = "192.168.4.1"
ESP32_PORT = 14550

MISSION = {
    "steps": [
        { "action": "arm" },
        { "action": "relay_on" },
        { "action": "wait",      "seconds": 5 },
        { "action": "relay_off" },
        { "action": "disarm" }
    ]
}

def send(msg):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        sock.sendto(msg.encode("utf-8"), (ESP32_IP, ESP32_PORT))
        print(f"  -> sent ({len(msg)} bytes)")
    except Exception as e:
        print(f"  X Failed: {e}")
        sys.exit(1)
    finally:
        sock.close()

print("=" * 45)
print("  ESP32 Arm + Relay Test  (no takeoff)")
print("=" * 45)

print("\n[1] Uploading test mission...")
send(json.dumps(MISSION, separators=(",", ":")))
time.sleep(1.5)  # give ESP32 time to fully deserialize before START arrives

print("[2] Sending START...")
send("START")

print("[3] Waiting for mission to complete (~20s)...")
print("    Watch ESP32 serial monitor for step logs.\n")
print("    Expected sequence:")
print("    arm -> wait 2s -> relay ON -> wait 5s -> relay OFF -> wait 1s -> disarm\n")
time.sleep(55)  # 30s max arm wait + 8s relay cycle + 10s disarm wait + buffer

print("[4] Done.")
print("    If arm was rejected, check Mission Planner params:")
print("      ARMING_CHECK = 0")
print("      SYSID_MYGCS  = 255")
print("      SERIAL1_PROTOCOL = 2")