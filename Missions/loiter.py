from mavsdk import System
from mavsdk.telemetry import LandedState
import asyncio

TAKEOFF_ALTITUDE = 5.0
LOITER_TIME = 30  # seconds


async def run():

    print("===================================")
    print("SIMPLE LOITER MISSION")
    print("===================================")

    # ==================================================
    # CONNECT
    # ==================================================

    drone = System()

    print("Connecting to drone...")

    await drone.connect(system_address="udpin://0.0.0.0:14550")

    async for state in drone.core.connection_state():

        if state.is_connected:
            print("Drone connected!")
            break

    # ==================================================
    # WAIT FOR GPS LOCK
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
    # SET TAKEOFF ALTITUDE
    # ==================================================

    await drone.action.set_takeoff_altitude(TAKEOFF_ALTITUDE)

    # ==================================================
    # TAKEOFF
    # ==================================================

    print(f"Taking off to {TAKEOFF_ALTITUDE} meters...")

    await drone.action.takeoff()

    # wait for stabilization
    await asyncio.sleep(8)

    # ==================================================
    # LOITER
    # ==================================================

    print(f"Loitering for {LOITER_TIME} seconds...")

    # Drone will automatically hold position
    await asyncio.sleep(LOITER_TIME)

    # ==================================================
    # RTL
    # ==================================================

    print("Returning to launch...")

    await drone.action.return_to_launch()

    # ==================================================
    # WAIT UNTIL LANDED
    # ==================================================

    print("Waiting for landing...")

    async for landed_state in drone.telemetry.landed_state():

        if landed_state == LandedState.ON_GROUND:
            print("Drone landed!")
            break

    # ==================================================
    # DISARM
    # ==================================================

    print("Disarming drone...")

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