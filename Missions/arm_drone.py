"""
ARM DRONE SCRIPT - JSON Mission Approach (ESP32 Mission Manager)
Sends ARM command as a JSON mission step, then starts execution

Usage:
    python arm_drone.py              # Arm the drone
    python arm_drone.py --disarm     # Disarm the drone
    python arm_drone.py --test       # Test connection only
"""

import socket
import json
import time
import argparse
import sys

# =========================================================
# ESP32 CONNECTION (Updated for esp_uploader.txt)
# =========================================================

ESP32_IP = "192.168.4.1"
UDP_PORT = 14550
TIMEOUT = 3

# =========================================================
# UDP HELPERS
# =========================================================

def send_udp(message, label=""):
    """Send UDP message to ESP32"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)
    
    try:
        if isinstance(message, dict):
            payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        else:
            payload = message.encode("utf-8") if isinstance(message, str) else message
        
        sock.sendto(payload, (ESP32_IP, UDP_PORT))
        label_str = f" ({label})" if label else ""
        print(f"  ✓ Sent {len(payload)} bytes{label_str}")
        return True
    except Exception as e:
        print(f"  ✗ Send failed: {e}")
        return False
    finally:
        sock.close()


def request_status():
    """Request status from ESP32"""
    print("Requesting status...")
    send_udp("STATUS", "Status request")
    print("  (Check ESP32 serial monitor for output)\n")


# =========================================================
# MISSION HELPERS
# =========================================================

def upload_mission(steps, label=""):
    """Upload mission to ESP32"""
    mission = {"steps": steps}
    label_str = f" {label}" if label else ""
    print(f"[1/2] Uploading mission{label_str}...")
    for i, step in enumerate(steps):
        action_info = f"{step['action']}"
        if "alt" in step:
            action_info += f" (alt={step['alt']}m)"
        if "lat" in step:
            action_info += f" (lat={step['lat']:.6f})"
        print(f"       {i+1}. {action_info}")
    
    if not send_udp(mission, "Mission JSON"):
        print("  ✗ Failed to upload mission")
        return False
    
    print("  ✓ Mission stored in ESP32\n")
    return True


def start_mission():
    """Start mission execution"""
    print("[2/2] Starting mission execution...")
    if not send_udp("START", "START command"):
        print("  ✗ Failed to start mission")
        return False
    
    print("  ✓ Mission started on ESP32\n")
    return True


# =========================================================
# DRONE OPERATIONS
# =========================================================

def arm_drone():
    """Arm the drone via JSON mission"""
    
    print("=" * 60)
    print("  ESP32 DRONE ARMING SCRIPT (Mission Manager)")
    print("=" * 60)
    print(f"Target: {ESP32_IP}:{UDP_PORT}\n")
    
    try:
        # Create ARM mission
        arm_mission = [
            {"action": "arm"}
        ]
        
        # Upload and execute
        if not upload_mission(arm_mission, "ARM"):
            sys.exit(1)
        
        if not start_mission():
            sys.exit(1)
        
        print("=" * 60)
        print("  STATUS: ARM mission sent ✓")
        print("  NEXT:   Watch ESP32 serial for confirmation")
        print("          Then take manual RC control")
        print("=" * 60)
        print()
        print("  Monitor ESP32 Serial:")
        print("    - [Step] ARM commanded...")
        print("    - [Step] Drone ARMED confirmed")
        print("\n  To disarm, run: python arm_drone.py --disarm\n")
    
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


def disarm_drone():
    """Disarm the drone via JSON mission"""
    
    print("=" * 60)
    print("  ESP32 DRONE DISARMING SCRIPT")
    print("=" * 60)
    print(f"Target: {ESP32_IP}:{UDP_PORT}\n")
    
    try:
        # Create DISARM mission
        disarm_mission = [
            {"action": "disarm"}
        ]
        
        # Upload and execute
        if not upload_mission(disarm_mission, "DISARM"):
            sys.exit(1)
        
        if not start_mission():
            sys.exit(1)
        
        print("=" * 60)
        print("  STATUS: DISARM mission sent ✓")
        print("=" * 60)
        print()
    
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


def check_connection():
    """Test connection by requesting status"""
    
    print("=" * 60)
    print("  ESP32 CONNECTION TEST")
    print("=" * 60)
    print(f"Target: {ESP32_IP}:{UDP_PORT}\n")
    
    request_status()


# =========================================================
# ENTRY POINT
# =========================================================

def main():
    parser = argparse.ArgumentParser(
        description="ARM DRONE - ESP32 Mission Manager Approach",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python arm_drone.py              # Arm the drone
  python arm_drone.py --disarm     # Disarm the drone
  python arm_drone.py --test       # Test connection only
        """
    )
    parser.add_argument("--disarm", action="store_true", help="Disarm the drone")
    parser.add_argument("--test", action="store_true", help="Test connection (request status)")
    
    args = parser.parse_args()
    
    if args.disarm:
        disarm_drone()
    elif args.test:
        check_connection()
    else:
        arm_drone()


if __name__ == "__main__":
    main()
