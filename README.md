# Autonomous Agricultural Drone System

An autonomous UAV spraying and payload control system built using:

* MAVSDK
* MAVLink
* PX4 / Pixhawk flight controllers
* ESP32-based relay payload controller
* Python Ground Control Station (GCS)

This project enables fully autonomous agricultural-style drone missions including:

* Autonomous takeoff
* GPS waypoint navigation
* Relay-controlled spraying/payload activation
* Slow-speed irrigation passes
* Loiter missions
* Automatic Return-To-Launch (RTL)
* Autonomous landing

---

# System Architecture

```text
                 ┌──────────────────────┐
                 │   Python GCS         │
                 │   (MAVSDK)           │
                 └─────────┬────────────┘
                           │ MAVLink UDP
                           │ Port 14550
                           ▼
                ┌──────────────────────┐
                │      ESP32 Bridge    │
                │  WiFi AP + GPIO4 Relay+ Pump │
                └─────────┬────────────┘
                          │ UART MAVLink
                          ▼
                ┌──────────────────────┐
                │      Pixhawk         │
                │ Flight Controller    │
                └─────────┬────────────┘


---

# Communication Pipeline

## 1. Python GCS → ESP32

The Python Ground Control Station communicates with the ESP32 using UDP packets over WiFi.

### Commands

```text
RELAY_ON
RELAY_OFF
```

---

## 2. ESP32 → Pixhawk

The ESP32 acts as a transparent MAVLink bridge.

### Connection

* UART Serial
* 57600 baud
* MAVLink passthrough

### ESP32 UART Pins

| ESP32 Pin | Pixhawk |
| --------- | ------- |
| GPIO16    | TX      |
| GPIO17    | RX      |

---

## 3. Relay Control

The ESP32 directly controls a relay module connected to:

| Function         | GPIO  |
| ---------------- | ----- |
| Relay Signal Pin | GPIO4 |

The relay is used to control:

* Irrigation pump
* Spraying mechanism
* Payload actuator
* External high-current systems

---

# Features

## Autonomous Flight

* Arm/disarm
* Takeoff
* Waypoint navigation
* RTL
* Landing
* Loiter missions

---

## Payload Control

* Relay ON/OFF during mission
* Synchronized spraying runs
* Waypoint-triggered payload activation

---

## ESP32 MAVLink Bridge

The ESP32 simultaneously:

* Broadcasts MAVLink packets over WiFi
* Receives MAVLink telemetry
* Processes UDP relay commands
* Controls external hardware

---

# Example Mission Flow

## Irrigation Mission

```text
1. Connect to drone
2. Wait for GPS lock
3. Arm drone
4. Takeoff to target altitude
5. Fly to Point A
6. Enable spraying relay
7. Fly slowly to Point B
8. Disable spraying relay
9. Return To Launch
10. Land automatically
11. Disarm
```

---

# Technologies Used

| Component | Purpose                                  |
| --------- | ---------------------------------------- |
| Python    | Ground control logic                     |
| MAVSDK    | Drone control API                        |
| MAVLink   | Drone communication protocol             |
| ESP32     | MAVLink WiFi bridge + payload controller |
| Pixhawk   | Flight controller                        |
| UDP       | Relay command transport                  |
| WiFi AP   | Wireless telemetry/control               |

---

# ESP32 WiFi Configuration

```cpp
SSID: MAVLink
Password: 12345678
```

Default ESP32 AP IP:

```text
192.168.4.1
```

UDP Port:

```text
14550
```

---

# Installation

## 1. Clone Repository

```bash
git clone <your-repository-url>
cd <project-folder>
```

---

## 2. Install Python Dependencies

```bash
pip install mavsdk
```

---

## 3. Upload ESP32 Firmware

Flash the ESP32 bridge code using:

* Arduino IDE
* PlatformIO
* VS Code + Arduino Extension

---

## 4. Connect Hardware

### ESP32 ↔ Pixhawk UART

| ESP32  | Pixhawk |
| ------ | ------- |
| GPIO16 | TX      |
| GPIO17 | RX      |
| GND    | GND     |

---

### Relay Wiring

| Relay Pin | Connection |
| --------- | ---------- |
| IN        | GPIO4      |
| VCC       | 3.3V / 5V  |
| GND       | GND        |

---

# Running Missions

## Loiter Mission

```bash
python loiter_mission.py
```

---

## Irrigation Mission

```bash
python irrigation_mission.py
```

---

# Safety Features

Current implemented safety behaviors:

* GPS lock verification before flight
* Automatic RTL
* Automatic landing
* Automatic disarm after landing
* Relay shutdown after mission completion

---

# Future Improvements

Planned/possible upgrades:

* Multi-lane field coverage
* Obstacle avoidance
* Terrain following
* Mission planner UI
* Telemetry dashboard
* Battery-aware mission aborts
* Autonomous recharge workflows
* Computer vision targeting
* Precision agriculture analytics

---

# Hardware Requirements

## Flight Stack

* Pixhawk flight controller
* GPS module
* Telemetry radio / WiFi

## Companion System

* ESP32
* Relay module
* Pump/payload hardware

## Power

* LiPo battery
* Voltage regulation for ESP32 + relay

---

# Important Notes

* Ensure common GND between ESP32 and relay module.
* Use appropriate voltage/current isolation for pumps.
* Always test missions in open environments.
* Verify failsafe behavior before autonomous operation.

---

# Project Goal

This project demonstrates a low-cost, modular autonomous drone architecture capable of real-world agricultural and payload automation workflows using open MAVLink-based systems.
