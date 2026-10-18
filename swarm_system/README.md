# swarm_system

Built around one hard constraint: **one Jetson per drone, each only ever
talks to its own Pixhawk.** A single process holding every drone's MAVSDK
connection doesn't match that hardware reality, so there's no central
multi-drone "coordinator" process anywhere in this system. Instead:

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
│   ├── core/
│   │   ├── mission_plan.py
│   │   ├── mission_parser.py
│   │   ├── action_runner.py
│   │   └── scheduler.py
│   ├── flight/
│   │   ├── telemetry_bus.py
│   │   ├── safety.py            # real SafetyWatchdog + SprayController
│   │   └── drone_agent.py       # single-drone, real safety checks, swarm-aware
│   ├── swarm/
│   │   ├── swarm_state.py       # in-memory view of other drones
│   │   └── mqtt_link.py         # registration + telemetry over MQTT
│   ├── plugins/
│   │   └── spray_pattern.py
│   ├── main.py                  # entry point
│   └── requirements.txt
│
└── missions/
    ├── mission_schema.xsd
    └── standalone_mission.xml   # fallback mission for offline / single-drone testing
```

## Key design decisions

1. **No central multi-drone coordinator.** One computer flying many drones
   over different connection strings is physically wrong for this setup —
   coordination is peer-to-peer over MQTT instead, never centralized.
2. **`SafetyWatchdog` / `SprayController` are real, working
   implementations** (`flight/safety.py`) — battery/geofence failsafes and
   the spray pump itself are fully wired, not stubs.
3. **A `swarm/` layer** gives every drone a live view of every other
   drone's telemetry (`SwarmState`), fed by `SwarmLink` over MQTT, and
   this is passed into action plugins via `drone_context["swarm_state"]`
   — this is the hook for "drones know what others are doing."
4. **ID assignment + mission handout is dynamic**, not hardcoded in one
   shared XML. `fleet_manager.py` maps `hardware_id -> drone_id` on first
   contact and pushes that drone's own mission file.
5. Core mission/scheduling internals (`mission_plan.py`, `mission_parser.py`,
   `action_runner.py`, `scheduler.py`, `telemetry_bus.py`,
   `spray_pattern.py`) are stable, solid pieces of the system.

## Mission schema v2 — generic, and now ground-scheduled

Two further changes landed after the above, both verified against real
SITL:

**1. The mission schema is now fully generic.** Waypoints nest inside
their owning `<Drone>` (not a flat sibling list cross-referenced by a
`drone_id` attribute). There's no built-in `spray_on`/`spray_off` baked
into the schema — every waypoint action is just `<Action code="...">`,
resolved to `plugins/<code>.py` at runtime by `core/action_runner.py`
(replaces `custom_code_runner.py`). Hardware wiring (GPIO pins, pump
timing) is gone from the mission entirely — it's local-only config in
each Jetson's `drone_config.yaml` under `hardware:`. A waypoint can carry
**multiple** `<Action>` elements, run in order. One shared
`fleet_manager/missions/swarm_mission.xml` now defines the whole swarm —
every drone registers and receives the identical file, each pulling out
only its own `<Drone>` block.

**2. Scheduling authority moved to the ground.** Previously each drone's
own local `core/scheduler.py` independently decided when to launch, off
its own clock, watching the mission's `<StartDate>`. Now `fleet_manager`
is the scheduling authority: it parses the shared mission's `<StartDate>`
once and broadcasts a single `swarm/launch` MQTT message to every drone
at that exact moment — real synchronized swarm takeoff from one
authoritative "go" signal, not N drones independently guessing. Each
drone's local scheduler stays armed the whole time as a **fallback only**
— if the ground link is down right at launch time, the drone still
launches itself off its own clock rather than waiting forever for a
signal that may never arrive. `_SingleDroneRunner` guards against both
triggers firing (whichever arrives first wins; the other is a no-op).

An operator can also trigger launch manually, ignoring `<StartDate>`
entirely:
```bash
python fleet_manager.py --broker-host localhost --launch-now
```

Verified live: armed a drone with a mission whose `<StartDate>` was 12
hours out (confirmed via log: local fallback scheduled, drone sitting
idle, not flying), then published one manual `swarm/launch` message —
the drone armed and took off within 10 seconds, proving the ground
signal genuinely overrides the local schedule rather than just agreeing
with it by coincidence.

## Running it now — against real SITL (verified working end to end)

This whole chain was actually run, not just written: two ArduCopter SITL
instances, fleet_manager, and two drone_node processes, flying real
(simulated) missions with custom plugin actions, battery telemetry, and
RTL/landing — confirmed via the logs, not assumed.

### SITL image: Mac vs Windows

The ArduPilot SITL Docker image is architecture-specific, so which one
you pull depends on the laptop:

| | **Mac (Apple Silicon)** | **Windows / Intel** |
|---|---|---|
| Image | `orthuk/ardupilot-sitl` (ARM-native) | an amd64-built SITL image |
| MAVLink output | `--out=udp:...` flags — any number of UDP destinations straight from the container, so `drone_node` and QGroundControl can both be connected to the same drone at once | a single MAVLink TCP port, which only serves **one client at a time** |
| Multiple drones + live map at once | Yes | Not without extra relay work — each container can only hand its one client to either `drone_node` or QGroundControl, not both, and that only gets worse per additional drone |
| Verified here | Fully — this is the setup below, run end to end with two drones | Single drone only, for now |

Everything from step 0 onward below is written for Mac/`orthuk`, since
that's the path actually run end to end — it's the primary, supported way
to run this. The Windows path is separate, below.

### Windows setup (single drone)

1. **Install Docker Desktop** (WSL2 backend) and pull an amd64-built
   ArduPilot SITL image — substitute its actual tag for `<SITL_IMAGE>`
   below (`orthuk/ardupilot-sitl`, used on Mac, is ARM-only and won't run
   here).
   ```powershell
   docker pull <SITL_IMAGE>
   ```
2. **Run one container**, exposing its MAVLink TCP port to the host:
   ```powershell
   docker run -d -i --name sitl1 -p 5760:5760 <SITL_IMAGE> ...
   ```
   (whatever startup command that image documents for a single ArduCopter
   instance). Unlike `orthuk`, this path doesn't give you multiple
   `--out=udp:...` destinations — it's one MAVLink TCP client on port
   `5760`, and only one.
3. **Pick one of these two modes per run — not both at once** (the port
   only serves a single client):

   **Mode A — fly the mission with `drone_node`:**
   ```yaml
   drone:
     connection_string: "tcp://127.0.0.1:5760"   # confirm this scheme against your mavsdk version
   ```
   ```powershell
   cd swarm_system\drone_node
   pip install -r requirements.txt
   python main.py --config config\drone_config_1.yaml ^
     --mission ..\missions\standalone_mission.xml --run-now
   ```
   No live map here — the port is already taken by `drone_node`, so a
   ground station can't connect to the same instance at the same time.

   **Mode B — watch/fly it manually in Mission Planner, no `drone_node`:**
   1. Leave `drone_node` disconnected.
   2. Open Mission Planner → top-right connection dropdown → select
      **TCP** → host `127.0.0.1`, port `5760` → **Connect**.
   3. Arm/fly it directly from Mission Planner's own controls to sanity
      check the SITL instance itself, independent of this codebase.

4. **One drone only, for now.** `fleet_manager` and the MQTT swarm layer
   work identically on any platform, but two Windows SITL containers
   still means two separate single-client ports with no way to watch
   either one live at the same time — stick to a single drone until a
   relay exists for sharing one TCP connection across multiple clients.

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
2. **Battery was reporting `9000%` instead of `90%`.** The original code
   did `remaining_percent * 100`, assuming a 0-1 fraction. Live SITL
   telemetry showed `remaining_percent` is already 0-100. Fixed in
   `flight/drone_agent.py`.
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

Waypoint flying uses `goto_location()` + a Python polling loop, not
MAVSDK's mission-upload API. This means the
Pixhawk itself never holds the route — if the Jetson process dies
mid-flight, the drone has no onboard fallback mission, just failsafes
(RC loss, battery). Fine for short local test flights; worth revisiting
before longer unattended missions.
