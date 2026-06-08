"""
ARM DRONE SCRIPT
Simple script to arm the drone via ESP32 bridge, then allow manual RC control

Usage:
    python arm_drone.py              # Connect, arm, and wait for manual control
    python arm_drone.py --disarm     # Disarm the drone
    python arm_drone.py --status     # Check drone status only
"""

from mavsdk import System
from mavsdk.telemetry import LandedState
import asyncio
import argparse
import sys

# =========================================================
# ESP32 CONNECTION (via UDP MAVLink Bridge)
# =========================================================

ESP32_IP = "192.168.4.1"
UDP_PORT = 14550

# =========================================================
# MAIN MISSION
# =========================================================

async def check_status(drone):
    """Check and display drone status"""
    
    async for state in drone.core.connection_state():
        if state.is_connected:
            print("✓ Drone connected!")
            break
    
    async for health in drone.telemetry.health():
        print(f"✓ Global Position: {health.is_global_position_ok}")
        print(f"✓ Home Position:   {health.is_home_position_ok}")
        print(f"✓ Gyroscope:       {health.is_gyroscope_ok}")
        print(f"✓ Accelerometer:   {health.is_accelerometer_ok}")
        print(f"✓ Magnetometer:    {health.is_magnetometer_ok}")
        print(f"✓ Barometer:       {health.is_barometer_ok}")
        break
    
    async for battery in drone.telemetry.battery():
        print(f"✓ Battery: {battery.remaining_percent * 100:.1f}%")
        break
    
    async for flight_mode in drone.telemetry.flight_mode():
        print(f"✓ Flight Mode: {flight_mode}")
        break
    
    async for armed in drone.telemetry.armed():
        status = "ARMED ✓" if armed else "DISARMED"
        print(f"✓ Status: {status}")
        break


async def run_arm():
    """Connect and arm the drone"""
    
    print("=" * 60)
    print("  ESP32 DRONE ARMING SCRIPT")
    print("=" * 60)
    print(f"Connecting to: {ESP32_IP}:{UDP_PORT}")
    print()
    
    # Create drone instance
    drone = System()
    
    try:
        # Connect to drone via ESP32 UDP bridge
        print("[1/5] Connecting to drone via ESP32...")
        await drone.connect(system_address=f"udpin://0.0.0.0:{UDP_PORT}")
        
        # Wait for connection
        async for state in drone.core.connection_state():
            if state.is_connected:
                print("      ✓ Connected!")
                break
        
        # Check health
        print("[2/5] Waiting for health checks...")
        async for health in drone.telemetry.health():
            if health.is_global_position_ok and health.is_home_position_ok:
                print("      ✓ GPS lock acquired!")
                break
        
        # Display status
        print("[3/5] Drone Status:")
        await check_status(drone)
        
        # Check if already armed
        async for armed in drone.telemetry.armed():
            if armed:
                print("\n[!] Drone is ALREADY ARMED")
                print("    Use 'python arm_drone.py --disarm' to disarm")
                return
        
        # ARM DRONE
        print("\n[4/5] Arming drone...")
        await drone.action.arm()
        print("      ✓ ARM command sent!")
        
        # Verify armed status
        await asyncio.sleep(1)
        
        async for armed in drone.telemetry.armed():
            if armed:
                print("      ✓ Drone ARMED successfully!")
            else:
                print("      ✗ Failed to arm - check preflight errors above")
                return
            break
        
        # Ready for control
        print("\n[5/5] Ready for testing!")
        print("=" * 60)
        print("  STATUS: ARMED ✓")
        print("  ACTION: Take manual control with RC transmitter")
        print("  Press Ctrl+C to disarm and exit")
        print("=" * 60)
        print()
        
        # Keep running - allow RC control
        # Monitor armed status and allow graceful exit
        try:
            while True:
                async for armed in drone.telemetry.armed():
                    if not armed:
                        print("\n[!] Drone disarmed externally")
                        return
                    break
                
                await asyncio.sleep(1)
        
        except KeyboardInterrupt:
            print("\n\n[DISARMING] Received interrupt signal...")
            await drone.action.disarm()
            print("           ✓ Disarm command sent")
            await asyncio.sleep(0.5)
    
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


async def run_disarm():
    """Disarm the drone"""
    
    print("Connecting to drone...")
    drone = System()
    
    try:
        await drone.connect(system_address=f"udpin://0.0.0.0:{UDP_PORT}")
        
        async for state in drone.core.connection_state():
            if state.is_connected:
                print("✓ Connected!")
                break
        
        print("Disarming drone...")
        await drone.action.disarm()
        print("✓ Disarm command sent!")
        
        await asyncio.sleep(1)
        
        async for armed in drone.telemetry.armed():
            if not armed:
                print("✓ Drone DISARMED successfully!")
            else:
                print("✗ Failed to disarm")
            break
    
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


async def run_status_check():
    """Check drone status without arming"""
    
    print("Connecting to drone...")
    drone = System()
    
    try:
        await drone.connect(system_address=f"udpin://0.0.0.0:{UDP_PORT}")
        
        async for state in drone.core.connection_state():
            if state.is_connected:
                print("✓ Connected!")
                break
        
        print("\nDrone Status:")
        await check_status(drone)
    
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


# =========================================================
# ENTRY POINT
# =========================================================

def main():
    parser = argparse.ArgumentParser(
        description="ARM DRONE - Simple arming script for testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python arm_drone.py              # Connect and arm
  python arm_drone.py --disarm     # Disarm only
  python arm_drone.py --status     # Check status only
        """
    )
    parser.add_argument("--disarm", action="store_true", help="Disarm the drone")
    parser.add_argument("--status", action="store_true", help="Check status only (don't arm)")
    
    args = parser.parse_args()
    
    if args.disarm:
        asyncio.run(run_disarm())
    elif args.status:
        asyncio.run(run_status_check())
    else:
        asyncio.run(run_arm())


if __name__ == "__main__":
    main()
