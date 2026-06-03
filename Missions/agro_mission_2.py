from mavsdk import System
from mavsdk.telemetry import LandedState
import asyncio
import socket

# ==================================================
# CONFIG
# ==================================================

TAKEOFF_ALTITUDE = 5.0

LOITER_TIME = 60      # total hover time
SPRAY_TIME = 20       # relay ON duration

ESP32_IP = "192.168.4.1"
UDP_PORT = 14550


async def run():

    print("===================================")
    print("LOITER + SPRAY MISSION")
    print("===================================")

    # ==================================================
    # CONNECT
    # ==================================================

    drone = System()

    print("Connecting to drone...")

    await drone.connect(system_address="udpin://0.0.0.0:14550")

    print("Waiting for connection...")

    async for state in drone.core.connection_state():

        if state.is_connected:
            print("Drone connected!")
            break

    # ==================================================
    # GPS LOCK
    # ==================================================

    print("Waiting for GPS lock...")

    async for health in drone.telemetry.health():

        if health.is_global_position_ok and health.is_home_position_ok:
            print("GPS lock acquired!")
            break

    # ==================================================
    # ARM
    # ==================================================

    print("Arming drone...")

    await drone.action.arm()

    print("Drone armed!")

    # ==================================================
    # TAKEOFF
    # ==================================================

    await drone.action.set_takeoff_altitude(TAKEOFF_ALTITUDE)

    print(f"Taking off to {TAKEOFF_ALTITUDE} m...")

    await drone.action.takeoff()

    # allow climb and stabilization
    await asyncio.sleep(10)

    # ==================================================
    # UDP SOCKET
    # ==================================================

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ==================================================
    # LOITER + SPRAY
    # ==================================================

    print("Beginning loiter...")

    mission_start = asyncio.get_event_loop().time()

    print("Relay ON")
    sock.sendto(b"RELAY_ON", (ESP32_IP, UDP_PORT))

    await asyncio.sleep(SPRAY_TIME)

    print("Relay OFF")
    sock.sendto(b"RELAY_OFF", (ESP32_IP, UDP_PORT))

    elapsed = asyncio.get_event_loop().time() - mission_start
    remaining = max(0, LOITER_TIME - elapsed)

    print(f"Continuing loiter for {remaining:.1f} seconds")

    await asyncio.sleep(remaining)

    # ==================================================
    # RTL
    # ==================================================

    print("Returning to launch...")

    await drone.action.return_to_launch()

    # ==================================================
    # WAIT FOR LANDING
    # ==================================================

    print("Waiting for landing...")

    async for landed_state in drone.telemetry.landed_state():

        if landed_state == LandedState.ON_GROUND:
            print("Drone landed!")
            break

    # ==================================================
    # DISARM
    # ==================================================

    try:
        await drone.action.disarm()
        print("Drone disarmed!")

    except Exception as e:
        print("Disarm failed:")
        print(e)

    print("===================================")
    print("MISSION COMPLETE")
    print("===================================")


asyncio.run(run())