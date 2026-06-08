# Autonomous Agricultural Drone System

An autonomous UAV spraying and payload control system built using:

* JSON-based mission management
* MAVLink protocol
* PX4 / Pixhawk flight controllers
* ESP32-based mission manager + relay payload controller
* Python Ground Control Station (GCS)

This project enables fully autonomous agricultural-style drone missions including:

* Autonomous arm/disarm
* GPS waypoint navigation with automatic retry logic
* Relay-controlled spraying/payload activation
* Variable-speed irrigation passes
* Automatic altitude management
* Return-To-Launch (RTL)
* Mission status monitoring

---

# System Architecture

## Mission Manager Architecture

The ESP32 stores and executes missions locally, providing robust autonomous operation:

```text
     ┌────────────────────────┐
     │   Python GCS           │
     │   mission_uploader.py  │
     │   mission_uploader2.py │
     │   arm_drone.py         │
     └──────────┬─────────────┘
                │ JSON mission + START/STOP
                │ UDP Port 14550
                ▼
     ┌────────────────────────┐
     │   ESP32 Mission Mgr    │
     │  - Stores mission      │
     │  - Executes locally    │
     │  - Retries on failure  │
     │  - GPIO4 Relay control │
     └──────────┬─────────────┘
                │ UART MAVLink (57600 baud)
                ▼
     ┌────────────────────────┐
     │   Pixhawk FCU          │
     │   Flight Controller    │
     └────────────────────────┘
```

---

# Mission System

## JSON Mission Format

Missions are defined as JSON with step-by-step actions:

```json
{
  "steps": [
    { "action": "arm" },
    { "action": "takeoff", "alt": 5 },
    { "action": "fly_to", "lat": 28.123456, "lon": 77.654321, "alt": 5 },
    { "action": "relay_on" },
    { "action": "fly_to", "lat": 28.123500, "lon": 77.654400, "alt": 5 },
    { "action": "relay_off" },
    { "action": "rtl" }
  ]
}
```

---

## Available Mission Actions

| Action | Parameters | Purpose |
|--------|-----------|---------|
| `arm` | none | Arm the drone (with retry logic) |
| `disarm` | none | Disarm the drone |
| `takeoff` | `alt` (meters) | Takeoff to specified altitude |
| `fly_to` | `lat`, `lon`, `alt` | Navigate to GPS waypoint (with automatic retry) |
| `relay_on` | none | Enable relay (sprayer/pump) |
| `relay_off` | none | Disable relay |
| `wait` | `seconds` | Wait for specified duration |
| `rtl` | none | Return To Launch |
| `land` | none | Land at current location |

---

## Mission Execution Features

* **Automatic Retries**: Waypoints are resent every 2 seconds if not reached
* **GPS Validation**: Waits for GPS lock before critical operations
* **Timeout Protection**: Each step has timeout safeguards (30s for arm, 45s for waypoint, etc.)
* **Relay Safety**: Relay automatically disabled on mission abort/timeout
* **Step Confirmation**: Each action is confirmed before proceeding

---

# Ground Control Station Scripts

## 1. arm_drone.py — Simple Arming

Arm the drone without full mission execution:

```bash
# Upload and execute ARM mission
python arm_drone.py

# Test connection
python arm_drone.py --test

# Disarm only
python arm_drone.py --disarm
```

**Output:**
```
============================================================
  ESP32 DRONE ARMING SCRIPT (Mission Manager)
============================================================
[1/2] Uploading mission ARM...
[2/2] Starting mission execution...
============================================================
  STATUS: ARM mission sent ✓
```

---

## 2. mission_uploader.py — Example Mission

Basic relay mission (takeoff → wait → spray → wait → rtl):

```bash
# Upload mission and prompt for confirmation
python mission_uploader.py

# Upload and immediately start
python mission_uploader.py --start

# Check mission status
python mission_uploader.py --status

# Emergency stop (trigger RTL)
python mission_uploader.py --stop
```

---

## 3. mission_uploader2.py — Agriculture Spray Mission

Complete spray mission template:

```bash
python mission_uploader2.py
```

**Mission Flow:**
1. Arm drone
2. Takeoff to 5m
3. Navigate to Point A (normal speed)
4. Enable relay (start spraying)
5. Navigate to Point B (slow speed ~1 m/s)
6. Disable relay (stop spraying)
7. Return To Launch (normal speed)

**Before Running:**

Edit these values in the script:
```python
POINT_A_LAT = 28.000000  # Start spraying here
POINT_A_LON = 77.000000
POINT_B_LAT = 28.000100  # End spraying here
POINT_B_LON = 77.000100
SPRAY_ALTITUDE = 5.0     # Altitude in meters
```

**Configure Drone for Slow Speed:**

In QGroundControl or Mission Planner, set:
- `WPNAV_SPEED` = 100 (1 m/s in cm/s)
- `WPNAV_SPEED_MAX` = 100

---

# UDP Command Protocol

## Uploading a Mission

Send JSON mission to `192.168.4.1:14550`:

```python
import socket
import json

mission = {
    "steps": [
        {"action": "arm"},
        {"action": "takeoff", "alt": 5},
        {"action": "rtl"}
    ]
}

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(json.dumps(mission).encode(), ("192.168.4.1", 14550))
sock.close()
```

## Starting a Mission

Send `"START"` to `192.168.4.1:14550`:

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(b"START", ("192.168.4.1", 14550))
sock.close()
```

## Stopping a Mission (Emergency RTL)

Send `"STOP"` to `192.168.4.1:14550`:

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(b"STOP", ("192.168.4.1", 14550))
sock.close()
```

## Checking Status

Send `"STATUS"` to request telemetry (output in ESP32 serial):

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(b"STATUS", ("192.168.4.1", 14550))
sock.close()
```

---

# Hardware Configuration

## ESP32 WiFi AP

```
SSID: ESP32_Drone_Network
Password: DronePassword123
IP: 192.168.4.1
UDP Port: 14550
```

## ESP32 ↔ Pixhawk UART

| ESP32 Pin | Pixhawk UART | Function |
|-----------|--------------|----------|
| GPIO16    | TX           | Serial RX |
| GPIO17    | RX           | Serial TX |
| GND       | GND          | Ground |

**Baud Rate:** 57600

## Relay Control

| Component | Connection |
|-----------|-----------|
| Relay IN  | ESP32 GPIO4 |
| Relay VCC | 3.3V / 5V |
| Relay GND | GND |
| Pump/Sprayer | Relay NO (normally open) |

---

# Installation

## 1. Upload ESP32 Firmware

Flash `esp_uploader.txt` to ESP32:

* Arduino IDE
* PlatformIO
* VS Code Arduino Extension

Required libraries:
- `WiFi` (built-in)
- `WiFiUdp` (built-in)
- `ArduinoJson` (install via library manager)
- `HardwareSerial` (built-in)

---

## 2. Connect Hardware

Connect ESP32 to Pixhawk UART2:
- ESP32 GPIO16 → Pixhawk TX
- ESP32 GPIO17 → Pixhawk RX
- Both GND together

Connect relay to ESP32 GPIO4

---

## 3. Install Python GCS

```bash
# No external dependencies needed for mission_uploader*.py
# Uses only standard library (socket, json, argparse)

# Optional: install for custom mission development
pip install python-can
```

---

# Safety Features

* **Mission Validation**: Checks for GPS lock before autonomous operations
* **Automatic Retries**: Failed waypoints retried for 45 seconds with 2s interval
* **Step Timeouts**: Each step has timeout protection (prevents infinite loops)
* **Relay Safety**: Relay shutdown on mission abort, RTL timeout, or emergency
* **Manual Failsafe**: STOP command triggers immediate RTL
* **Arming Checks**: Pre-flight checks required before arm
* **Preflight Diagnostics**: Checks ARMING_CHECK, SYSID_MYGCS parameters

---

# Troubleshooting

## Mission Doesn't Start

1. Check ESP32 is powered on
2. Verify WiFi connection: Connect to "ESP32_Drone_Network"
3. Check UDP port: Ensure 14550 not blocked
4. Monitor ESP32 serial: Watch for "[UDP] Mission stored OK"

## Drone Won't Arm

Check ESP32 serial for:
```
[Step] ARM timeout — check ARMING_CHECK=0, SYSID_MYGCS=255
```

**Solution:**
In QGroundControl:
- Set `ARMING_CHECK` = 0 (disable preflight checks) or fix reported errors
- Set `SYSID_MYGCS` = 255 (match ESP32's MY_SYS_ID)

## Relay Not Working

1. Verify GPIO4 connection
2. Check relay module power (3.3V or 5V)
3. Test with: `{"steps": [{"action": "relay_on"}, {"action": "wait", "seconds": 5}, {"action": "relay_off"}]}`

## GPS Lock Fails

- Ensure GPS antenna connected properly
- Wait 60+ seconds for initial GPS lock
- Check for RF interference
- Try clear sky location

---

# Example Missions

## Quick Test (1 minute)

```python
"steps": [
    {"action": "arm"},
    {"action": "takeoff", "alt": 2},
    {"action": "wait", "seconds": 10},
    {"action": "rtl"}
]
```

## Full Spray Mission (5-10 minutes)

```python
"steps": [
    {"action": "arm"},
    {"action": "takeoff", "alt": 5},
    {"action": "fly_to", "lat": 28.000000, "lon": 77.000000, "alt": 5},
    {"action": "relay_on"},
    {"action": "fly_to", "lat": 28.000100, "lon": 77.000100, "alt": 5},
    {"action": "relay_off"},
    {"action": "rtl"}
]
```

---

# Future Improvements

* Multi-point spray patterns
* Autonomous field coverage planning
* Mission optimization for battery life
* Telemetry graphing dashboard
* Web-based mission planner
* Obstacle avoidance integration
* Computer vision targeting

---

# Important Notes

* Ensure common GND between ESP32 and relay module.
* Use appropriate voltage/current isolation for pumps.
* Always test missions in open environments.
* Verify failsafe behavior before autonomous operation.

---

# Project Goal

This project demonstrates a low-cost, modular autonomous drone architecture capable of real-world agricultural and payload automation workflows using open MAVLink-based systems.
