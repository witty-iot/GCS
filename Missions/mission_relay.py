from mavsdk import System
import asyncio
import socket

ESP32_IP = "192.168.4.1"
UDP_PORT = 14550


async def run():

    print("Connecting to drone...")

    drone = System()

    await drone.connect(system_address="udpin://0.0.0.0:14550")

    print("Waiting for connection...")

    async for state in drone.core.connection_state():

        if state.is_connected:
            print("Drone connected!")
            break

    # ==================================================
    # ARM DRONE
    # ==================================================

    print("Arming drone...")

    try:
        await drone.action.arm()
        print("Drone armed!")

    except Exception as e:
        print("ARM timeout but drone may still be armed")
        print(e)

    # WAIT AFTER ARM
    await asyncio.sleep(2)

    # ==================================================
    # UDP SOCKET
    # ==================================================

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ==================================================
    # RELAY ON
    # ==================================================

    print("Turning relay ON")
    sock.sendto(b"RELAY_ON", (ESP32_IP, UDP_PORT))

    # KEEP RELAY ON FOR 5 SECONDS
    await asyncio.sleep(30)

    # ==================================================
    # RELAY OFF
    # ==================================================

    print("Turning relay OFF")
    sock.sendto(b"RELAY_OFF", (ESP32_IP, UDP_PORT))

    # small safety delay
    await asyncio.sleep(1)

    # ==================================================
    # DISARM DRONE (IMPORTANT)
    # ==================================================

    print("Disarming drone...")

    try:
        await drone.action.disarm()
        print("Drone disarmed!")
    except Exception as e:
        print("DISARM failed or timeout")
        print(e)

    print("Mission complete")


asyncio.run(run())