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

## How everything works

### What runs where

| Machine | What runs | Needs MAVSDK? | Talks to Pixhawk? |
|---|---|---|---|
| **Your laptop** | `fleet_manager/fleet_manager.py` + an MQTT broker (`mosquitto`) | No | No |
| **Each Jetson** (one per drone) | `drone_node/main.py` | Yes | Yes — its own, only |

The laptop is pure coordination: it assigns drone IDs, pushes each drone's
mission file (and that mission's action metadata — see below), broadcasts
the swarm launch signal, and shows a live dashboard. It never opens a
MAVSDK/MAVLink connection. Every bit of flight logic — connecting to the
Pixhawk, arming, navigating, spraying, failsafes — runs on the Jetson, and
only on that Jetson's own drone.

### MQTT pub/sub — how the pieces actually talk

MQTT is the only thing connecting the laptop to the Jetsons, and Jetsons
to each other. There's no other network call anywhere in this system (no
REST API, no shared database, no direct socket between drones). A
`mosquitto` broker runs once, on the laptop; everyone else
(`fleet_manager.py` and every `drone_node`) is just an MQTT client of it.
Drones never talk directly to each other — all messages pass through the
broker, keyed by topic name.

Topics, defined in `drone_node/swarm/mqtt_link.py` and mirrored in
`fleet_manager/fleet_manager.py`:

| Topic | Published by | Subscribed by | Purpose |
|---|---|---|---|
| `swarm/register/request` | `drone_node` | `fleet_manager` | "I'm this hardware — who am I and what's my mission?" |
| `swarm/register/ack/<hardware_id>` | `fleet_manager` | the one `drone_node` with that `hardware_id` | Assigned `drone_id` + the mission XML + that mission's action metadata |
| `swarm/telemetry/<drone_id>` | `drone_node`, every ~2s | every other `drone_node` (wildcard) **and** `fleet_manager` | Live position/state broadcast — the entire swarm-awareness mechanism |
| `swarm/launch` | `fleet_manager`, at the mission's `<StartDate>` (or `--launch-now`) | every `drone_node` | One authoritative "go" signal for synchronized swarm takeoff |

Each piece degrades independently: if the broker is unreachable, a
`drone_node` falls back to its locally configured `fallback_mission` and
flies standalone with an empty `SwarmState` (zero peers, no error).
Single-drone operation requires none of this infrastructure.

### Mission file + action system

- **One shared mission file.** `fleet_manager/missions/swarm_mission.xml`
  defines the whole swarm; every drone registers and receives the
  identical file, then pulls out only its own `<Drone>` block.
- **Actions are fully generic.** A waypoint's `<Action code="...">` is
  just a name — `core/action_runner.py` resolves it to
  `plugins/<code>.py` at runtime. Nothing is hardcoded (no built-in
  `spray_on`/`spray_off` baked into the schema); adding a new action is
  dropping in a new plugin file.
- **Action parameters never live in the mission XML.** Each plugin's
  tuning (e.g. `spray_pattern`'s pattern/passes) lives in a sidecar
  `plugins/<code>.meta.json` file, and that metadata travels to the drone
  *with* the mission over MQTT at registration time (bundled into the
  registration ack by `fleet_manager`) — a Jetson always gets the
  ground's current params, not a possibly-stale local copy.
- **Multiple actions per waypoint**, wrapped in `<Actions mode="sequential
  | parallel">` — run one after another, or launch them all at once.
- Hardware wiring (GPIO pins, pump timing) is never mission data — it's
  local-only config in each Jetson's `drone_config.yaml` under
  `hardware:`.

### Scheduling — ground-authoritative, with a local fallback

`fleet_manager` is the scheduling authority: it parses the shared
mission's `<StartDate>` and broadcasts one `swarm/launch` MQTT message to
every drone at that exact moment — real synchronized takeoff from one
signal, not each drone independently guessing. Each drone's own local
scheduler (`core/scheduler.py`) stays armed the whole time as a
**fallback only** — if the ground link is down right at launch time, the
drone still launches itself off its own clock rather than waiting forever
for a signal that may never arrive. An operator can also trigger launch
manually, ignoring `<StartDate>` entirely:
```bash
python fleet_manager.py --broker-host localhost --launch-now
```

### Drone-down notifications

`fleet_manager` tracks a last-seen timestamp for every registered drone.
If a drone goes silent for longer than `--heartbeat-timeout-sec` (default
15s), it's flagged DOWN and an alert fires — always logged locally, and
also posted to a Teams channel if `--webhook-url` (or the
`SWARM_NOTIFY_WEBHOOK` env var) is set. A matching alert fires when
telemetry resumes. The live dashboard also shows a `Link: OK/DOWN` column
per drone.

### Directory structure

```
swarm_system/
├── fleet_manager/                      # ───── LAPTOP ─────
│   ├── fleet_manager.py                  Registry, mission + metadata push, ground
│   │                                      scheduler, heartbeat watchdog, live dashboard.
│   ├── notifier.py                       Drone-down/recovered alerts (console + Teams).
│   ├── missions/swarm_mission.xml        The one shared mission file for the whole swarm.
│   ├── registry.json                     Auto-created/updated: hardware_id -> drone_id.
│   └── requirements.txt                  Just paho-mqtt — deliberately lightweight.
│
├── drone_node/                         # ───── JETSON (copy this whole folder per drone) ─────
│   ├── main.py                           Entry point: registers, parses the mission,
│   │                                      wires up DroneAgent + SwarmLink + SwarmState, flies.
│   ├── config/
│   │   └── drone_config.yaml             Per-Jetson hardware facts that never travel over
│   │                                      the network: hardware_id, broker IP, connection
│   │                                      string, mavsdk_server_port, local hardware wiring.
│   ├── core/                             Mission data model + parsing + action resolution +
│   │   ├── mission_plan.py               scheduling — no MQTT, no swarm awareness here.
│   │   ├── mission_parser.py
│   │   ├── action_runner.py              Resolves <Action code> to plugins/<code>.py,
│   │   │                                  loads its meta.json (or network-delivered
│   │   │                                  metadata), runs it — sync or async.
│   │   └── scheduler.py                  Local fallback scheduler (see above).
│   ├── flight/                           Everything that actually talks to the Pixhawk.
│   │   ├── drone_agent.py                Flight loop for THIS drone only: connect ->
│   │   │                                  preflight -> arm/takeoff -> fly each waypoint ->
│   │   │                                  run its actions -> RTL/land.
│   │   ├── safety.py                     SafetyWatchdog (battery/geofence) + SprayController
│   │   │                                  (real Jetson.GPIO, or a logged simulation).
│   │   └── telemetry_bus.py              In-process async pub/sub (local only — NOT MQTT).
│   ├── swarm/                             The ONLY code that talks MQTT.
│   │   ├── mqtt_link.py                  SwarmLink: registration, telemetry, launch signal.
│   │   └── swarm_state.py                In-memory view of every other drone's telemetry.
│   ├── plugins/
│   │   ├── spray_on.py / spray_off.py / spray_pattern.py / take_photo.py
│   │   └── *.meta.json                    Per-action parameters, sidecar to each plugin.
│   └── requirements.txt
│
└── missions/
    ├── mission_schema.xsd
    └── standalone_mission.xml             Fallback mission for offline / single-drone testing.
```

### End to end

1. **Boot.** Each Jetson runs `python main.py --config config/drone_config.yaml`
   independently — nothing coordinates process start; one per drone,
   whenever that drone powers on.
2. **Registration.** `SwarmLink` connects to the broker and publishes
   `swarm/register/request` with this Jetson's `hardware_id`.
3. **ID + mission + metadata handout.** `fleet_manager` resolves
   `hardware_id -> drone_id` (assigning a new one if unseen, persisted to
   `registry.json`), bundles the shared mission XML with every referenced
   action's current metadata, and acks it back on that drone's own topic.
   The Jetson caches both locally and is done needing the laptop.
4. **No broker / no ack / `--mission` passed.** Falls back to
   `drone_config.yaml: fallback_mission` — the drone flies standalone,
   `SwarmState` simply never gets peer updates. Same code path as a full
   swarm, just zero peers.
5. **Launch.** The drone waits for either the ground `swarm/launch`
   broadcast or its own local `<StartDate>` fallback — whichever fires
   first wins, the other is a no-op.
6. **Flight.** `DroneAgent` connects to this Jetson's own Pixhawk, arms,
   takes off, flies each waypoint in order, runs that waypoint's actions
   (sequential or parallel), then RTLs/lands.
7. **Telemetry out/in.** Every ~2s, `DroneAgent` publishes telemetry to
   `swarm/telemetry/<drone_id>`. Every other `drone_node` is subscribed to
   the wildcard, so a plugin on *this* drone can query what *other* drones
   are doing right now via `drone_context["swarm_state"]`, with no central
   decision-maker in the loop.
8. **The laptop's view.** `fleet_manager` renders the live dashboard from
   that same telemetry wildcard, and its heartbeat watchdog uses it to
   detect and alert on a drone going silent.

### Key design decisions

1. **No central multi-drone coordinator** — coordination is peer-to-peer
   over MQTT, never one process holding every drone's flight connection.
2. **`SafetyWatchdog` / `SprayController` are real, working
   implementations** — battery/geofence failsafes and the spray pump
   itself are fully wired, not stubs.
3. **Action code never travels over the network** — only its metadata
   does. Plugin `.py` files are deployed to every Jetson via git, the same
   as any other software update, not pushed per-mission. Shipping
   executable code over MQTT would mean a drone blindly runs whatever
   arrives on the wire.
4. **ID assignment + mission handout is dynamic**, not hardcoded.

### Known gaps, called out on purpose

- Waypoint flying uses `goto_location()` + a Python polling loop, not
  MAVSDK's mission-upload API — the Pixhawk itself never holds the route,
  so if the Jetson process dies mid-flight there's no onboard fallback
  mission, just failsafes (RC loss, battery). Fine for short local test
  flights; worth revisiting before longer unattended missions.
- The fleet dashboard never expires a drone's last-seen telemetry row —
  cosmetic only, the heartbeat watchdog's DOWN/OK status is accurate
  regardless.
- `mavsdk` v4 is a breaking API change (replaced the old `System()` gRPC
  API with a different native binding) — `requirements.txt` pins
  `mavsdk<4.0.0`.

## Setup — Mac

Two ArduCopter SITL instances,
`fleet_manager`, and two `drone_node` processes, flying real (simulated)
missions with action plugins, battery telemetry, and RTL/landing —
confirmed via the logs, not assumed.

### 0. Install Docker + the MQTT broker (one-time)

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
pushing the shared `missions/swarm_mission.xml` plus action metadata.

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
   the real waypoints, climb to altitude, and RTL home.

### 5. Adding a new drone to the swarm

1. Flash/boot its Jetson, copy the `drone_node/` folder onto it.
2. Set a unique `hardware_id` in its `drone_config.yaml`, point `broker.host`
   at your laptop.
3. Add a `<Drone id="...">` block for it in `fleet_manager/missions/swarm_mission.xml`.
4. Run `python main.py` on that Jetson. Done — no changes to any other
   drone or to fleet_manager's code.

## Setup — Windows

The ArduPilot SITL Docker image is architecture-specific — `orthuk/ardupilot-sitl`
used above is ARM-native and won't run on Windows/Intel. An amd64-built
SITL image is needed instead, and it behaves differently: it exposes a
single MAVLink **TCP** port that only serves **one client at a time**,
instead of the Mac image's `--out=udp:...` flags that can feed any number
of destinations at once. Practical effect: on Windows, `drone_node` and a
ground station can't both be connected to the same drone simultaneously,
and that only gets worse per additional drone — so for now, **single
drone only**.

1. **Install Docker Desktop** (WSL2 backend) and pull the amd64 SITL
   image:
   ```powershell
   docker pull radarku/ardupilot-sitl
   ```
2. **Run one container**, exposing its MAVLink TCP port to the host:
   ```powershell
   docker run -d -i --name sitl1 -p 5760:5760 radarku/ardupilot-sitl ...
   ```
   (whatever startup command that image documents for a single ArduCopter
   instance).
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

4. `fleet_manager` and the MQTT swarm layer work identically on any
   platform — but two Windows SITL containers still means two separate
   single-client ports with no way to watch either one live at the same
   time, so stick to one drone until a relay exists for sharing a single
   TCP connection across multiple clients.
