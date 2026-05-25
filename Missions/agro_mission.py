from mavsdk import System
from mavsdk.telemetry import LandedState
import asyncio
import socket
import math

# =========================================================
# ESP32 RELAY CONFIG
# =========================================================

ESP32_IP = "192.168.4.1"
UDP_PORT = 14550

# =========================================================
# MISSION PARAMETERS
# =========================================================

TAKEOFF_ALTITUDE = 10.0          # meters

# ---------------------------------------------------------
# POINT A  (START SPRAY)
# Replace with your real coordinates
# ---------------------------------------------------------

POINT_A_LAT = 28.000000
POINT_A_LON = 77.000000

# ---------------------------------------------------------
# POINT B (END SPRAY)
# Drone will fly slowly from A -> B while spraying
# ---------------------------------------------------------

POINT_B_LAT = 28.000100
POINT_B_LON = 77.000100

# =========================================================
# SPEED SETTINGS
# =========================================================

NORMAL_SPEED = 5.0      # m/s
SPRAY_SPEED = 1.0       # VERY SLOW spraying speed

# =========================================================
# HELPERS
# =========================================================

async def wait_until_arrival(drone, target_lat, target_lon):

    while True:

        async for position in drone.telemetry.position():

            current_lat = position.latitude_deg
            current_lon = position.longitude_deg

            distance = get_distance_meters(
                current_lat,
                current_lon,
                target_lat,
                target_lon
            )

            print(f"Distance to target: {distance:.2f} meters")

            # ARRIVAL THRESHOLD
            if distance < 2.0:
                print("Target reached!")
                return

            break

        await asyncio.sleep(1)


def get_distance_meters(lat1, lon1, lat2, lon2):

    R = 6371000  # Earth radius in meters

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(delta_lambda / 2.0) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


# =========================================================
# MAIN MISSION
# =========================================================

async def run():

    print("========================================")
    print("AUTONOMOUS IRRIGATION MISSION")
    print("========================================")

    # =====================================================
    # CONNECT TO DRONE
    # =====================================================

    drone = System()

    print("Connecting to drone...")

    await drone.connect(system_address="udpin://0.0.0.0:14550")

    async for state in drone.core.connection_state():

        if state.is_connected:
            print("Drone connected!")
            break

    # =====================================================
    # WAIT FOR GPS LOCK
    # =====================================================

    print("Waiting for global position estimate...")

    async for health in drone.telemetry.health():

        if health.is_global_position_ok and health.is_home_position_ok:
            print("GPS lock acquired!")
            break

    # =====================================================
    # UDP SOCKET FOR ESP32
    # =====================================================

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # =====================================================
    # ARM
    # =====================================================

    print("Arming drone...")

    await drone.action.arm()

    print("Drone armed!")

    # =====================================================
    # SET TAKEOFF ALTITUDE
    # =====================================================

    await drone.action.set_takeoff_altitude(TAKEOFF_ALTITUDE)

    # =====================================================
    # TAKEOFF
    # =====================================================

    print(f"Taking off to {TAKEOFF_ALTITUDE} meters...")

    await drone.action.takeoff()

    # Wait for stabilization
    await asyncio.sleep(12)

    # =====================================================
    # FLY TO POINT A
    # =====================================================

    print("Flying to Point A...")

    await drone.action.set_current_speed(NORMAL_SPEED)

    await drone.action.goto_location(
        POINT_A_LAT,
        POINT_A_LON,
        TAKEOFF_ALTITUDE,
        0
    )

    await wait_until_arrival(
        drone,
        POINT_A_LAT,
        POINT_A_LON
    )

    # =====================================================
    # START SPRAYING
    # =====================================================

    print("Turning relay ON (START SPRAY)...")

    sock.sendto(b"RELAY_ON", (ESP32_IP, UDP_PORT))

    await asyncio.sleep(1)

    # =====================================================
    # SLOW SPRAY PASS
    # =====================================================

    print("Flying slowly from Point A -> Point B")

    await drone.action.set_current_speed(SPRAY_SPEED)

    await drone.action.goto_location(
        POINT_B_LAT,
        POINT_B_LON,
        TAKEOFF_ALTITUDE,
        0
    )

    await wait_until_arrival(
        drone,
        POINT_B_LAT,
        POINT_B_LON
    )

    # =====================================================
    # STOP SPRAYING
    # =====================================================

    print("Turning relay OFF (STOP SPRAY)...")

    sock.sendto(b"RELAY_OFF", (ESP32_IP, UDP_PORT))

    await asyncio.sleep(1)

    # =====================================================
    # RETURN TO LAUNCH
    # =====================================================

    print("Returning to launch...")

    await drone.action.return_to_launch()

    # =====================================================
    # WAIT UNTIL LANDED
    # =====================================================

    print("Waiting for landing...")

    async for landed_state in drone.telemetry.landed_state():

        if landed_state == LandedState.ON_GROUND:
            print("Drone landed!")
            break

    # =====================================================
    # DISARM
    # =====================================================

    print("Disarming drone...")

    try:
        await drone.action.disarm()
        print("Drone disarmed!")
    except Exception as e:
        print("Disarm failed:")
        print(e)

    print("========================================")
    print("MISSION COMPLETE")
    print("========================================")


# =========================================================
# START
# =========================================================

asyncio.run(run())