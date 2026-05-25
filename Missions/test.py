print("SCRIPT STARTED")

from mavsdk import System
import asyncio


async def run():

    print("Inside async function")

    drone = System()

    print("Connecting to MAVLink UDP stream...")

    # Listen for MAVLink packets on UDP port 14550
    await drone.connect(system_address="udp://:14550")

    print("Waiting for drone connection...")

    async for state in drone.core.connection_state():

        print("Connection State:", state)

        if state.is_connected:
            print("Drone connected successfully!")
            break

    print("Reading telemetry...")

    # Read one flight mode message
    async for mode in drone.telemetry.flight_mode():
        print("Flight Mode:", mode)
        break

    # Read one battery message
    async for battery in drone.telemetry.battery():
        print("Battery Remaining:", battery.remaining_percent)
        break

    print("Sending ARM command...")

    try:
        await drone.action.arm()
        print("ARM command sent!")
    except Exception as e:
        print("ARM failed:", e)

    print("Sending TAKEOFF command...")

    try:
        await drone.action.takeoff()
        print("TAKEOFF command sent!")
    except Exception as e:
        print("TAKEOFF failed:", e)


asyncio.run(run())