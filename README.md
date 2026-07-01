# Autonomous Agricultural Drone GCS

Ground Control Station and ESP32 mission manager for an autonomous agricultural drone. The current architecture separates raw MAVLink forwarding from high-level mission control:

- Python GCS scripts upload JSON missions over UDP.
- ESP32 stores and executes the mission locally.
- ESP32 talks to the Pixhawk over UART using MAVLink.
- ESP32 exposes live mission status back to the GCS.
- ESP32 also bridges raw MAVLink telemetry/control packets over a separate UDP port.

## Current Architecture

```text
Python GCS / Laptop
  - Missions/mission_uploader.py
  - Missions/mission_uploader2.py
  - Missions/mission_uploader_test.py
  - Mission JSON, START, STOP, STATUS
          |
          | WiFi UDP to ESP32 AP
          | Mission API: 192.168.4.1:14551
          | MAVLink bridge: 192.168.4.1:14550
          v
ESP32 Mission Manager
  - WiFi AP: ESP32_Drone_Network
  - Stores uploaded JSON mission
  - Executes mission step-by-step
  - Sends MAVLink commands to Pixhawk
  - Reads Pixhawk heartbeat/GPS/altitude/mode/armed state
  - Controls sprayer relay on GPIO4
          |
          | UART2 MAVLink, 57600 baud
          | ESP32 RX2 GPIO16 <- Pixhawk TX
          | ESP32 TX2 GPIO17 -> Pixhawk RX
          v
Pixhawk / ArduPilot Flight Controller
  - Arms/disarms
  - Changes modes
  - Takes off, flies waypoints, lands/RTL
```

## Network And Ports

| Purpose | IP / Port | Used By |
| --- | --- | --- |
| ESP32 WiFi AP | `192.168.4.1` | Laptop connects to this network |
| Mission command API | UDP `192.168.4.1:14551` | Python mission scripts |
| MAVLink bridge | UDP `192.168.4.1:14550` | QGroundControl / Mission Planner / MAVLink clients |
| ESP32 serial monitor | USB serial `115200` baud | Firmware logs |
| Pixhawk UART link | UART `57600` baud | ESP32 <-> Pixhawk MAVLink |

ESP32 AP credentials:

```text
SSID: ESP32_Drone_Network
Password: DronePassword123
```

## Repository Layout

```text
Arduino_esp_code/
  esp_uploader.txt          ESP32 firmware / mission manager

Missions/
  mission_uploader.py       Guided takeoff, relay, loiter, land mission
  mission_uploader2.py      Agriculture spray mission template
  mission_uploader_test.py  No-takeoff arm + relay + disarm test
  mode_test.py              Older/minimal mode-switch test
  relay_test.py             Older/minimal relay-only test

README.md
pyproject.toml
```

Note: the active mission scripts use the current mission API port `14551`. The older minimal `mode_test.py` and `relay_test.py` currently default to `14550`; change their `ESP32_PORT` to `14551` before using them with the current ESP32 firmware.

## Mission Command Protocol

All mission commands are sent to:

```text
UDP 192.168.4.1:14551
```

### Upload Mission

Send a JSON object with a `steps` array. The ESP32 replies:

```text
OK MISSION_STORED
```

Example:

```python
import json
import socket

mission = {
    "steps": [
        {"action": "set_mode", "mode": "GUIDED"},
        {"action": "takeoff", "alt": 2.5},
        {"action": "relay_on"},
        {"action": "wait", "seconds": 5},
        {"action": "relay_off"},
        {"action": "land"}
    ]
}

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(3)
sock.sendto(json.dumps(mission).encode("utf-8"), ("192.168.4.1", 14551))
print(sock.recvfrom(2048)[0].decode())
sock.close()
```

### Start Mission

Send:

```text
START
```

Expected reply:

```text
OK START
```

The ESP32 refuses start if no mission is stored, a mission is already running, or there is no fresh Pixhawk heartbeat.

### Stop Mission

Send:

```text
STOP
```

Expected reply:

```text
OK STOP
```

Current firmware treats STOP as an abort and commands `LAND`.

### Request Status

Send:

```text
STATUS
```

The ESP32 replies with JSON:

```json
{
  "type": "status",
  "mission_stored": true,
  "running": false,
  "completed": false,
  "aborted": false,
  "step": 0,
  "total_steps": 8,
  "action": "set_mode",
  "gps": true,
  "gps_age_ms": 150,
  "heartbeat_age_ms": 200,
  "lat": 28.000000,
  "lon": 77.000000,
  "alt": 2.5,
  "armed": false,
  "mode": 5,
  "last_abort": ""
}
```

The Python scripts use this status response for preflight checks and terminal monitoring.

## Supported Mission Actions

| Action | Parameters | What It Does |
| --- | --- | --- |
| `set_mode` | `mode` | Requests ArduPilot mode: `GUIDED`, `LOITER`, `RTL`, `LAND`, `AUTO`, `MANUAL` |
| `arm` | none | Arms the drone and waits for heartbeat confirmation |
| `disarm` | none | Disarms the drone and waits for confirmation |
| `takeoff` | `alt` | Switches to GUIDED, arms, and commands takeoff to altitude in meters |
| `fly_to` | `lat`, `lon`, `alt` | Sends waypoint command and waits until within radius/altitude tolerance |
| `relay_on` | none | Turns ESP32 GPIO4 HIGH |
| `relay_off` | none | Turns ESP32 GPIO4 LOW |
| `wait` | `seconds` | Holds the current mission step for the given time |
| `rtl` | none | Commands Return To Launch |
| `land` | none | Commands LAND |

## Mode Changes

Yes, mode changes are still done through the `set_mode` mission action.

Example mission step:

```json
{ "action": "set_mode", "mode": "GUIDED" }
```

The Python GCS does not directly switch modes itself. It uploads this JSON step to the ESP32, and the ESP32 firmware converts the mode name into an ArduPilot custom mode value before sending a MAVLink `SET_MODE` message to the Pixhawk.

Supported mode names in the current ESP32 firmware:

| Mode Name | ArduPilot Custom Mode |
| --- | --- |
| `MANUAL` | `0` |
| `AUTO` | `3` |
| `GUIDED` | `4` |
| `LOITER` | `5` |
| `RTL` | `6` |
| `LAND` | `9` |

The current `Missions/mission_uploader.py` mission uses `set_mode` to enter `GUIDED`, then later `LOITER`, then `LAND`.

## Main Scripts

### `Missions/mission_uploader.py`

Default mission:

1. Set `GUIDED`
2. Take off to `2.5 m`
3. Relay ON
4. Set `LOITER`
5. Wait `5 s`
6. Relay OFF
7. Set `LAND`
8. Land

Commands:

```bash
python Missions/mission_uploader.py
python Missions/mission_uploader.py --start
python Missions/mission_uploader.py --status
python Missions/mission_uploader.py --stop
python Missions/mission_uploader.py --ip 192.168.4.1 --port 14551
```

### `Missions/mission_uploader2.py`

Agriculture spray mission template:

1. Arm
2. Take off to `SPRAY_ALTITUDE`
3. Fly to Point A
4. Relay ON
5. Fly slowly to Point B
6. Relay OFF
7. RTL

Before running, edit:

```python
POINT_A_LAT = 0.000000
POINT_A_LON = 0.000000
POINT_B_LAT = 0.000100
POINT_B_LON = 0.000100
SPRAY_ALTITUDE = 2.5
```

Run:

```bash
python Missions/mission_uploader2.py
python Missions/mission_uploader2.py --start
python Missions/mission_uploader2.py --status
python Missions/mission_uploader2.py --stop
```

For slow spray speed, configure the flight controller before the mission, for example:

```text
WPNAV_SPEED = 100
WPNAV_SPEED_MAX = 100
```

### `Missions/mission_uploader_test.py`

No-takeoff bench/field safety test:

1. Arm
2. Relay ON
3. Wait `5 s`
4. Relay OFF
5. Disarm

Run:

```bash
python Missions/mission_uploader_test.py
```

This script waits for fresh heartbeat telemetry, uploads the mission, starts it, and monitors status until completion or abort.

## ESP32 Firmware

Firmware file:

```text
Arduino_esp_code/esp_uploader.txt
```

Upload it to the ESP32 using Arduino IDE, PlatformIO, or a VS Code Arduino workflow.

Required Arduino libraries:

- `WiFi`
- `WiFiUdp`
- `ArduinoJson`
- `HardwareSerial`

Important firmware settings:

```cpp
#define RELAY_PIN   4
#define RXD2        16
#define TXD2        17
#define SERIAL_BAUD 57600

const int MAVLINK_UDP_PORT = 14550;
const int MISSION_UDP_PORT = 14551;
const uint8_t MY_SYS_ID = 255;
const uint8_t MY_COMP_ID = 190;
const uint8_t DRONE_SYS_ID = 1;
const uint8_t DRONE_COMP_ID = 1;
```

## Hardware Wiring

### ESP32 To Pixhawk

| ESP32 | Pixhawk UART | Purpose |
| --- | --- | --- |
| GPIO16 RX2 | Pixhawk TX | Pixhawk -> ESP32 MAVLink |
| GPIO17 TX2 | Pixhawk RX | ESP32 -> Pixhawk MAVLink |
| GND | GND | Common ground |

Set the Pixhawk serial port connected to the ESP32 to MAVLink at `57600` baud.

### Relay / Sprayer

| Relay Module | Connection |
| --- | --- |
| IN | ESP32 GPIO4 |
| VCC | Relay-rated 3.3V or 5V supply |
| GND | Common ground |
| Load | Pump/sprayer circuit through relay contacts |

Use proper isolation and power handling for pump current. Do not power a pump directly from the ESP32.

## Preflight Flow

The active Python scripts do this before mission start:

1. Upload JSON mission.
2. Wait for `OK MISSION_STORED`.
3. Poll `STATUS`.
4. Confirm a mission is stored.
5. Confirm no mission is already running.
6. Confirm fresh Pixhawk heartbeat, and for flight missions, fresh GPS.
7. Send `START`.
8. Monitor `STATUS` until completed, aborted, or timed out.

Fresh telemetry means GPS and/or heartbeat age is below `5000 ms`.

## Safety Behavior

- Relay is forced OFF when a mission completes or aborts.
- STOP commands abort the mission and command LAND.
- ARM retries for up to `30 s`.
- Waypoints are resent every `2 s`.
- Waypoint timeout is `45 s`.
- GPS/heartbeat freshness is checked before navigation.
- Takeoff timeout is `20 s`.
- Unsupported mode names are skipped by the ESP32 firmware.
- Arming checks remain controlled by the Pixhawk/ArduPilot; the GCS does not bypass them.

## Troubleshooting

### No UDP Reply

- Connect the laptop to `ESP32_Drone_Network`.
- Check the mission scripts are targeting `192.168.4.1:14551`.
- Watch the ESP32 serial monitor at `115200` baud.
- Confirm the firmware prints both ports:

```text
MAVLink UDP: 14550  Mission UDP: 14551
```

### `ERROR no_fresh_heartbeat`

- Check ESP32 GPIO16/GPIO17 wiring to Pixhawk TX/RX.
- Confirm common ground.
- Confirm Pixhawk serial port protocol is MAVLink.
- Confirm baud is `57600`.
- Power-cycle after changing serial parameters.

### Preflight Never Gets GPS

- Move outside with clear sky.
- Wait for GPS lock.
- Check GPS module and antenna.
- Confirm Pixhawk is publishing `GLOBAL_POSITION_INT`.

### Mission Aborts During Takeoff

- Check `last_abort` in the status output.
- Make sure arming checks pass in Mission Planner/QGroundControl.
- Confirm the vehicle can enter GUIDED and accept takeoff.
- Verify altitude telemetry is updating.

### Relay Does Not Switch

- Verify relay IN is connected to GPIO4.
- Confirm relay VCC/GND and common ground.
- Run `mission_uploader_test.py` for a simple arm/relay/disarm path, or fix `relay_test.py` to use port `14551` for a relay-only test.

## Development Notes

The Python mission scripts use only standard-library networking modules for mission upload/status:

- `socket`
- `json`
- `argparse`
- `time`
- `sys`

`pyproject.toml` currently lists additional project dependencies such as `mavsdk`, but the current mission uploader scripts do not require them for UDP mission control.

## Project Goal

This project demonstrates a low-cost, modular drone control architecture where a laptop uploads high-level agricultural missions, the ESP32 executes them locally, and the Pixhawk remains responsible for flight control and failsafe behavior.
