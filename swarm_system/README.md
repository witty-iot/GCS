# swarm_system

Dismantled and rebuilt from `drone_brain`. The core difference: `drone_brain`
assumed one process on one machine holds every drone's MAVSDK connection
(`SwarmCoordinator` + N `DroneAgent`s in one Python process). That doesn't
match your actual hardware — **one Jetson per drone, each only ever talks
to its own Pixhawk.** So the multi-drone "coordinator" is gone. Instead:

- Every Jetson runs its own independent `drone_node` process, flying only
  its own mission, against only its own Pixhawk.
- Drones find out about each other over MQTT — a lightweight broker
  (running on your laptop) relays telemetry between drones and hands out
  drone IDs + mission files.
- **This also means a single drone works with zero code changes** — if
  there's no broker and no other drones, a `drone_node` just flies its own
  mission standalone. Multi-drone is additive, not required.

## What runs where

| Machine | What runs | Needs MAVSDK? |
|---|---|---|
| **Your laptop** | `fleet_manager/fleet_manager.py` + an MQTT broker (`mosquitto`) | No |
| **Each Jetson** | `drone_node/main.py` | Yes |

The laptop never touches a Pixhawk. It only assigns IDs, hands out mission
files, and shows a live dashboard. All flight logic lives on the Jetson.

## Directory structure

```
swarm_system/
├── fleet_manager/            # LAPTOP
│   ├── fleet_manager.py       # registry + mission push + dashboard
│   ├── missions/               # one XML per drone_id: drone-01.xml, drone-02.xml, ...
│   ├── registry.json           # auto-created: hardware_id -> drone_id mapping
│   └── requirements.txt
│
├── drone_node/                # JETSON (copy this whole folder to each one)
│   ├── config/
│   │   └── drone_config.yaml   # EDIT per Jetson: hardware_id, broker IP, serial port
│   ├── core/                   # kept from drone_brain, unchanged
│   │   ├── mission_plan.py
│   │   ├── mission_parser.py
│   │   ├── custom_code_runner.py
│   │   └── scheduler.py
│   ├── flight/
│   │   ├── telemetry_bus.py    # kept from drone_brain, unchanged
│   │   ├── safety.py            # NEW — real SafetyWatchdog + SprayController
│   │   └── drone_agent.py       # REWRITTEN — single-drone, real safety checks, swarm-aware
│   ├── swarm/
│   │   ├── swarm_state.py       # NEW — in-memory view of other drones
│   │   └── mqtt_link.py         # NEW — registration + telemetry over MQTT
│   ├── plugins/
│   │   └── spray_pattern.py    # kept from drone_brain, unchanged
│   ├── main.py                  # NEW entry point
│   └── requirements.txt
│
└── missions/
    ├── mission_schema.xsd
    └── standalone_mission.xml   # fallback mission for offline / single-drone testing
```

## What changed vs. drone_brain, concretely

1. **`SwarmCoordinator` deleted.** It modeled one computer flying many
   drones over different connection strings — physically wrong for your
   setup. Coordination is now peer-to-peer over MQTT, not centralized.
2. **`SafetyWatchdog` / `SprayController` now actually exist and work**
   (`flight/safety.py`). In drone_brain these were imports of modules that
   were never included, so battery/geofence failsafes and the pump itself
   silently did nothing.
3. **New `swarm/` layer** gives every drone a live view of every other
   drone's telemetry (`SwarmState`), fed by `SwarmLink` over MQTT, and
   this is passed into custom-code plugins via `drone_context["swarm_state"]`
   — this is the hook for "drones know what others are doing."
4. **ID assignment + mission handout is now dynamic**, not hardcoded in one
   shared XML. `fleet_manager.py` maps `hardware_id -> drone_id` on first
   contact and pushes that drone's own mission file.
5. Everything else (`mission_plan.py`, `mission_parser.py`,
   `custom_code_runner.py`, `scheduler.py`, `telemetry_bus.py`,
   `spray_pattern.py`) is kept as-is from `drone_brain` — it was solid.

## Running it now — against real SITL (verified working end to end)

This whole chain was actually run, not just written: two ArduCopter SITL
instances, fleet_manager, and two drone_node processes, flying real
(simulated) missions with custom plugin actions, battery telemetry, and
RTL/landing — confirmed via the logs, not assumed.

### 0. Install Docker + the MQTT broker (laptop only, one-time)

```bash
brew install mosquitto
# Docker Desktop: brew install --cask docker, then open it once manually
docker pull orthuk/ardupilot-sitl
```

### 1. Start SITL — one container per drone

```bash
mosquitto -p 1883 -v &   # leave running

docker run -d -i --name sitl1 orthuk/ardupilot-sitl \
  ./Tools/autotest/sim_vehicle.py -v ArduCopter -I 0 -w --no-rebuild \
  --no-extra-ports \
  --out=udp:host.docker.internal:14540 \
  --out=udp:host.docker.internal:14550 \
  --custom-location=28.6139,77.2090,0,0

docker run -d -i --name sitl2 orthuk/ardupilot-sitl \
  ./Tools/autotest/sim_vehicle.py -v ArduCopter -I 1 -w --no-rebuild \
  --no-extra-ports \
  --out=udp:host.docker.internal:14541 \
  --out=udp:host.docker.internal:14551 \
  --custom-location=28.6150,77.2102,0,0
```

**Two non-obvious flags that actually matter, found by testing this, not guessed:**
- `-d -i` (not just `-d`) — without `-i`, MAVProxy hits stdin EOF
  immediately in a detached container and crash-loops forever (you'll see
  `Unloading module ...` repeating in `docker logs` with no progress).
  Keeping stdin open fixes it.
- **Wait for full settle before arming** — `docker logs sitl1` will show
  `AP: ArduPilot Ready` within ~10s, but EKF/GPS variance needs another
  10-20s to fully converge. Arming too early fails with
  `ActionError: FAILED: 'Failed'; origin: arm()` or `takeoff()`. Wait for
  `AP: EKF3 IMU0 is using GPS` in the logs before connecting, or just
  give it ~25-30s after container start.

### 2. Laptop — start fleet_manager

```bash
cd swarm_system/fleet_manager
pip install -r requirements.txt
python fleet_manager.py --broker-host localhost --broker-port 1883
```

It prints a live fleet table (empty until a drone registers) and will
assign `drone-01`, `drone-02`, ... to each new `hardware_id` it sees,
pushing whatever mission file exists at `missions/<drone_id>.xml`.
(Known cosmetic gap: the dashboard never expires a drone's last-seen
telemetry, so a drone that already finished and exited still shows its
final reading forever — harmless, just don't read a stale row as "still
flying.")

### 3a. One drone, no network at all (works today, no broker needed)

```bash
cd swarm_system/drone_node
pip install -r requirements.txt
python main.py --config config/drone_config_1.yaml \
  --mission ../missions/standalone_mission.xml --run-now
```

`--mission` skips registration entirely — useful for bench testing a
single Jetson or a SITL instance with nothing else running. Drop
`--run-now` to honor the mission's `<StartDate>` instead.

### 3b. Two drones, registering with fleet_manager (the swarm demo)

Each drone needs **its own `drone_config.yaml`** — `config/drone_config_1.yaml`
and `config/drone_config_2.yaml` already exist, pointed at `sitl1`/`sitl2`
respectively. The two fields that must differ per drone:
```yaml
hardware_id: jetson-01                        # unique per drone
drone:
  connection_string: "udpin://0.0.0.0:14540"   # this drone's SITL port
  mavsdk_server_port: 50051                    # MUST be unique per drone
                                                # running on the SAME machine
```

**Why `mavsdk_server_port` matters, found by testing this:** every
`mavsdk.System()` spawns a local `mavsdk_server` on a *fixed default port
(50051)*. Running two `drone_node` processes on one laptop without
setting distinct ports causes the second one's client to silently
connect to the *first* drone's server — not a connection error, just
wrong telemetry (drone 2 reporting drone 1's GPS position). This only
matters when multiple drones run on one machine for SITL testing; one
real Jetson per drone never hits it.

```bash
cd swarm_system/drone_node
python main.py --config config/drone_config_1.yaml --run-now &
python main.py --config config/drone_config_2.yaml --run-now &
```

First run: it asks fleet_manager "who am I?", gets back `drone-01` and the
contents of `fleet_manager/missions/drone-01.xml`, caches it locally
under `missions_cache/`, and flies it. Run a second Jetson with
`hardware_id: jetson-02` in its config and it gets `drone-02` with
`fleet_manager/missions/drone-02.xml` automatically — no code changes.

Drop `--run-now` to honor the mission's `<StartDate>` instead (the
scheduler waits and fires at that exact time — see the "do I need to keep
it plugged in" conversation: power the Jetson up a few minutes ahead of
`StartDate`, it doesn't need to sit idle for hours).

### 4. Watching it on a map (QGroundControl)

`fleet_manager`'s dashboard is text only. To see an actual moving drone
icon on a map, connect a ground station to one of the second `--out`
ports from step 1 — `14550` for drone 1, `14551` for drone 2.

**Use QGroundControl, not Mission Planner, on a Mac** — Mission Planner
is .NET/Windows-native and doesn't run well on macOS. QGroundControl is
cross-platform and natively supports multiple simultaneous vehicles on
one map.

1. Install QGroundControl (download from qgroundcontrol.com, or
   `brew install --cask qgroundcontrol` if available).
2. Open it, go to **Application Settings → Comm Links**, add a UDP link
   listening on port `14550` (and a second one on `14551` for drone 2 —
   QGC will show both vehicles on the same map simultaneously).
3. Run the mission as in step 3b. You'll see both drone icons move along
   the real waypoints, climb to altitude, and RTL home — same as a real
   flight, because QGC can't tell the difference.

### 5. Adding a new drone to the swarm

1. Flash/boot its Jetson, copy the `drone_node/` folder onto it.
2. Set a unique `hardware_id` in its `drone_config.yaml`, point `broker.host`
   at your laptop.
3. Drop a mission file for it at `fleet_manager/missions/drone-0N.xml`
   (copy an existing one, change the `<Drone id>` and waypoints).
4. Run `python main.py` on that Jetson. Done — no changes to any other
   drone or to fleet_manager's code.

### 6. Checking swarm-awareness is actually working

With two drones running against `--simulate` SITL instances (or two real
Jetsons), each one's log will show `peers_visible=N` on startup growing as
others register, and `flight/drone_agent.py` passes `swarm_state` into
every `custom_code` plugin's `drone_context` — a plugin can call
`drone_context["swarm_state"].peers()` or
`.any_peer_critical_battery(20)` to react to what other drones are doing.
`plugins/spray_pattern.py` doesn't use this yet (it didn't need to); it's
the hook to build reactive swarm behavior on top of, deliberately kept
simple rather than over-built before you know which reactions you
actually need.

## Bugs this SITL run actually caught (this is the point of testing before buying hardware)

1. **`mavsdk` v4 is a breaking API change.** The installed-by-default
   version replaced the old `System()` gRPC API with a different native
   binding. `requirements.txt` now pins `mavsdk<4.0.0` — without this
   pin, every `System()` call in `drone_agent.py` throws a `TypeError`
   immediately.
2. **Battery was reporting `9000%` instead of `90%`.** drone_brain's
   original code did `remaining_percent * 100`, assuming a 0-1 fraction.
   Live SITL telemetry showed `remaining_percent` is already 0-100.
   Fixed in `flight/drone_agent.py`.
3. **Two drones on one laptop silently cross-talked telemetry.** Every
   `mavsdk.System()` spawns a local `mavsdk_server` on a fixed default
   port (50051); running two `drone_node` processes without distinct
   ports made the second one connect to the first drone's server,
   reporting drone 1's GPS position as drone 2's. Fixed by adding
   `mavsdk_server_port` to `drone_config.yaml` (see step 3b above). This
   is a SITL-on-one-machine-only issue — doesn't exist with one real
   Jetson per drone.

None of these would have been obvious from reading the code — they only
showed up by actually connecting to a running flight controller, which is
exactly the value of doing this on SITL before buying a Jetson.

## Known gap, called out on purpose

Waypoint flying uses `goto_location()` + a Python polling loop (inherited
from drone_brain), not MAVSDK's mission-upload API. This means the
Pixhawk itself never holds the route — if the Jetson process dies
mid-flight, the drone has no onboard fallback mission, just failsafes
(RC loss, battery). Fine for short local test flights; worth revisiting
before longer unattended missions.
