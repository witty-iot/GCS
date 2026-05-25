from mavsdk import System
import asyncio


async def run():

    print("Connecting to drone...")

    drone = System()

    # Listen for MAVLink UDP packets
    await drone.connect(system_address="udpin://0.0.0.0:14550")

    print("Waiting for drone connection...")

    async for state in drone.core.connection_state():

        print("Connection State:", state)

        if state.is_connected:
            print("Drone connected successfully!")
            break

    # Read current flight mode
    async for mode in drone.telemetry.flight_mode():
        print("Flight Mode:", mode)
        break

    # ARM COMMAND
    print("Sending ARM command...")

    try:
        await drone.action.arm()
        print("Drone armed successfully!")

    except Exception as e:
        print("ARM failed:")
        print(e)


asyncio.run(run())