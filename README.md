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
| MAVLink bridge (logger copy) | UDP broadcast `192.168.4.255:14552` | `Missions/flight_logger.py` — always-broadcast duplicate of 14550, on its own port, so it works even while Mission Planner/QGC holds 14550 exclusively |
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
  esp_uploader.txt              ESP32 firmware / mission manager

Missions/
  mission_uploader.py           Guided takeoff, relay, loiter, land mission
  mission_uploader2.py          Guided MAV_CMD_DO_REPOSITION test
  mission_uploader_test.py      No-takeoff arm + relay + disarm test
  mission_hover_guided_test.py  Pure-Guided hover test (no mode switch, no relay)
  mission_hover_loiter_test.py  Guided takeoff + Loiter-hold hover test (no relay)
  mission_brake_hover_test.py   Guided local offset move + zero-velocity brake test
  mission_local_ned_test.py     Guided local offset position-hold test

docs/
  guided-mode-drift-diagnosis.md  Diagnosis of in-flight drift/instability issues

README.md
pyproject.toml
```

All mission scripts use the mission API port `14551`.

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
  "last_abort": "",
  "last_statustext": "",
  "last_statustext_severity": 255,
  "last_important_text": "",
  "last_important_severity": 255
}
```

`last_statustext` is the single most recent Pixhawk STATUSTEXT of any
severity — routine chatter (GPS/EKF NOTICE/INFO/DEBUG messages) overwrites it
constantly. `last_important_text`/`last_important_severity` only update on
severity <= WARNING (EMERGENCY..WARNING), so a real `PreArm: ...`/arm-reject
reason survives that chatter until a script actually reads it, instead of
being silently clobbered before the next `STATUS` poll.

The Python scripts use this status response for preflight checks and terminal monitoring.

## Supported Mission Actions

Two mission styles are supported and can be freely mixed within one mission:
pure-Guided steps that drive the vehicle directly via MAVLink position/velocity
targets (`reposition`, `fly_to`, `local_ned`, `brake_hover`), and `set_mode`,
which hands control to one of ArduPilot's own flight modes (e.g. `LOITER`)
using its own tuned position-hold controller instead.

| Action | Parameters | What It Does |
| --- | --- | --- |
| `set_mode` | `mode` | Requests ArduPilot mode: `GUIDED`, `LOITER`, `RTL`, `LAND`, `AUTO`, `MANUAL` |
| `arm` | none | Arms the drone and waits for heartbeat confirmation |
| `disarm` | none | Disarms the drone and waits for confirmation |
| `takeoff` | `alt` | Switches to GUIDED, arms, and commands takeoff to altitude in meters |
| `fly_to` | `lat`, `lon`, `alt` | Sends an absolute Guided position target and waits until within radius/altitude tolerance |
| `reposition` | `lat`, `lon`, `alt` | Sends `MAV_CMD_DO_REPOSITION` and waits until within radius/altitude tolerance |
| `local_ned` | `x` (north m), `y` (east m), `z` (down m), `seconds` | Computes an absolute target that many meters from wherever the vehicle is when the step starts, flies there, then holds for `seconds` |
| `brake_hover` | `seconds` | Holds a zero-velocity Guided target (resent every 500ms) for `seconds` |
| `relay_on` | none | Turns ESP32 GPIO4 HIGH (relay is wired only to the ESP32, not the Pixhawk) |
| `relay_off` | none | Turns ESP32 GPIO4 LOW |
| `wait` | `seconds` | Holds the current mission step for the given time — sends no commands, so in Guided mode the vehicle simply keeps its last position target |
| `rtl` | none | Commands Return To Launch |
| `land` | none | Commands LAND |

`local_ned`'s `x`/`y`/`z` are a one-time North/East/Down offset from the
vehicle's position when the step starts — the ESP32 converts that offset to
a fixed absolute lat/lon/alt once, then resends *that* fixed target, the same
way `fly_to`/`reposition` do. It deliberately does not send the offset itself
as a repeating `MAV_FRAME_LOCAL_OFFSET_NED` message, since ArduPilot
interprets that frame as "relative to the vehicle's position right now" on
every message received — resending it periodically previously caused a
runaway drift in the direction of the offset. See
`docs/guided-mode-drift-diagnosis.md` for the full writeup.

## Mode Changes

Yes, mode changes are still done through the `set_mode` mission action.

Example mission step:

```json
{ "action": "set_mode", "mode": "GUIDED" }
```

The Python GCS does not directly switch modes itself. It uploads this JSON step to the ESP32, and the ESP32 firmware converts the mode name into an ArduPilot custom mode value before sending it via `MAV_CMD_DO_SET_MODE` (the modern, recommended mode-change command — the firmware previously used the deprecated MAVLink1 `SET_MODE` message).

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

Guided `MAV_CMD_DO_REPOSITION` test:

1. Arm
2. Take off to `TARGET_ALTITUDE`
3. Reposition to `TARGET_LAT`/`TARGET_LON`
4. Wait `HOLD_SECONDS`
5. Land

Before running, edit `TARGET_LAT`/`TARGET_LON` to a nearby field coordinate.
The script refuses to start if the target is farther than
`--max-start-distance` (default 25m) from the current GPS fix, as a safety
check against a stale/wrong coordinate — override with `--force-distance`
only in a clear field.

Run:

```bash
python Missions/mission_uploader2.py
python Missions/mission_uploader2.py --start
python Missions/mission_uploader2.py --status
python Missions/mission_uploader2.py --stop
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

### `Missions/mission_hover_guided_test.py` and `Missions/mission_hover_loiter_test.py`

A matched pair of simple flight tests, no relay involved, used to isolate
whether in-flight drift tracks the flight mode or is common to both:

- `mission_hover_guided_test.py`: arm → takeoff to `1.5m` → hold `5s` purely
  in GUIDED, actively via a `local_ned` step with a zero north/east/down
  offset (the ESP32 resends the same absolute position target every 2s
  instead of just hoping the vehicle stays on whatever takeoff left it on)
  → land.
- `mission_hover_loiter_test.py`: same flight, but switches to `LOITER` for
  the hold instead of staying in GUIDED.

If only the LOITER-hold flight drifts, the mode transition (or LOITER
itself) is implicated. If both drift by a similar amount, the cause is
common to both — most likely EKF/position-estimate quality (compass
interference, vibration), since every ArduCopter mode holds position using
the same EKF estimate. See `docs/guided-mode-drift-diagnosis.md`.

Run either the same way:

```bash
python Missions/mission_hover_guided_test.py --start
python Missions/mission_hover_loiter_test.py --start
```

Both write a detailed timestamped log to `logs/` on every run (success,
abort, or Ctrl+C) — see Flight Logs below. Both also accept `--force` to skip
the script's own GPS-fix wait for indoor testing (Pixhawk's own arming checks
still run) — see Preflight Never Gets GPS under Troubleshooting.

### `Missions/mission_brake_hover_test.py` and `Missions/mission_local_ned_test.py`

Guided-mode local-offset tests: arm → takeoff → `local_ned` move (5m north)
→ either `brake_hover` (zero-velocity hold) or land directly. Useful for
exercising the `local_ned`/`brake_hover` actions specifically.

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
- **Never run `arm`/`takeoff` mission steps on a bench without propellers removed
  AND the vehicle physically restrained.** A `takeoff` step arms and commands a
  real climb; ArduPilot's altitude controller sees no climb from bare motors and
  keeps increasing throttle trying to reach the target altitude, potentially all
  the way to max — this is normal ArduPilot behavior, not a bug, and no
  script/firmware change here prevents it. With propellers attached and
  unrestrained, this step would fly. Bench-test comms/arming with Mission
  Planner's Motor Test tool (bounded, short, per-motor) instead of a full
  autonomous mission.
- `send_start()`/`send_stop()` retry a lost UDP ack up to 3x, and `send_start()`
  falls back to a `STATUS` query before ever declaring failure — a dropped ack
  does not mean the vehicle didn't arm/start; see Firmware Reliability Notes.

## Firmware Reliability Notes

- All ESP32 debug logging goes through `debugf()`/`debugln()` instead of
  `Serial.print*` directly. Plain `Serial` calls block once the USB TX
  buffer fills if no serial monitor is draining it — with debug logging on
  nearly every parsed MAVLink message, that could freeze `loop()` entirely
  mid-flight (no more STATUS replies, no more command resends, no more
  abort/timeout checks). `debugf()`/`debugln()` check
  `Serial.availableForWrite()` first and silently drop the line instead of
  blocking, so a full USB buffer can never stall flight control.
- `EKF_STATUS_REPORT` flags are read from the correct byte offset (20, after
  five variance floats — verified against the generated MAVLink headers) — a
  previous offset bug made `ekf_flags` in `STATUS` replies meaningless.
- `STATUSTEXT` messages (e.g. the human-readable PreArm/arm-reject reason)
  are now parsed and exposed as `last_statustext` /
  `last_statustext_severity` in `STATUS`, so an `ARM rejected by autopilot`
  abort shows *why* without needing Mission Planner/QGC connected.
- Since routine GPS/EKF chatter (severity NOTICE/INFO/DEBUG) shares the same
  `last_statustext` field and arrives continuously, it can overwrite a real
  PreArm/arm-reject message before a script's next `STATUS` poll ever reads
  it. `last_important_text`/`last_important_severity` track a second buffer
  that only updates on severity <= WARNING, so the real reason survives.
- `sendUdpText("OK START")` fires before `executeMission()` ever runs, so
  structurally it should never be slower than the ARM/mode-switch
  `COMMAND_ACK`s that follow — but it's still a single UDP datagram with no
  delivery guarantee, and doubling outbound traffic via `LOGGER_UDP_PORT`
  right as Pixhawk ramps up post-arm telemetry can be enough to drop it in
  a burst. A lost `OK START` does **not** mean the mission didn't start: the
  Python scripts' `send_start()` retries, and if still inconclusive, checks
  `STATUS` directly rather than assuming failure and exiting on an armed,
  flying vehicle.

## Flight Logs

`mission_hover_guided_test.py` and `mission_hover_loiter_test.py` write a
detailed timestamped log to `logs/` every time they run — on success,
failure, abort, or Ctrl+C — via `Missions/flight_logger.py`. In addition to
the JSON `STATUS` fields the terminal already prints, the logger listens on
the ESP32's dedicated logger broadcast (UDP `14552` — see Network And Ports)
and decodes it directly, so the log captures things `STATUS` never exposed:
every `STATUSTEXT` ArduCopter sent (the same messages Mission Planner's
Messages tab shows, not just the single most recent one), every arm/disarm
and mode change, every `COMMAND_ACK` result, and periodic
GPS/battery/EKF/vibration snapshots.

Notes:
- This is passive/best-effort: it only listens, it never sends anything to
  the vehicle. `14552` is a fixed always-broadcast duplicate of the MAVLink
  stream that Mission Planner/QGC never touches, specifically so it keeps
  working even while Mission Planner is connected and holding `14550`
  exclusively (Windows lets a .NET UDP socket like Mission Planner's bind a
  port exclusively, which blocks any other process — including this logger —
  from also binding that same port). If the ESP32 hasn't been reflashed with
  this dedicated-port firmware yet, pass `--mav-port 14550` to fall back to
  the old shared port (works, but only when Mission Planner/QGC isn't
  connected at the same time). Either way, if the port can't be opened for
  any reason, the log still gets written with the script's own milestone
  notes, just without the raw Pixhawk message capture.
- `logs/*.log` is gitignored — these are per-run artifacts, not source.

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

- Move outside with clear sky. Indoors, `gps_satellites` will sit at `0` and
  `gps_fix_type` will never reach 3 — this is expected, not a Pixhawk
  rejection, and the script's own local wait for a GPS fix will time out
  every time no matter how long you wait.
- Wait for GPS lock.
- Check GPS module and antenna.
- Confirm Pixhawk is publishing `GLOBAL_POSITION_INT`.
- To actually reach an ARM attempt indoors (e.g. to see Pixhawk's *real*
  PreArm reason instead of just a local timeout), run with `--force` — this
  skips only the script's own GPS-fix wait; Pixhawk's own arming checks are
  never bypassed and will still reject ARM if it isn't actually ready.

### Mission Aborts During Takeoff

- Check `last_abort` in the status output.
- Make sure arming checks pass in Mission Planner/QGroundControl.
- Confirm the vehicle can enter GUIDED and accept takeoff.
- Verify altitude telemetry is updating.

### Relay Does Not Switch

- Verify relay IN is connected to GPIO4.
- Confirm relay VCC/GND and common ground.
- Run `mission_uploader_test.py` for a simple arm/relay/disarm path (no takeoff).
- If drift/instability appears whenever the relay is on (e.g. driving a pump), see the EKF/EMI discussion in `docs/guided-mode-drift-diagnosis.md` — relay/pump switching that isn't wired to the Pixhawk can still corrupt the compass or vibration readings the Pixhawk's own EKF depends on.

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
