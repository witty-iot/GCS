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

## Architecture

### What runs where

| Machine | What runs | Needs MAVSDK? | Talks to Pixhawk? |
|---|---|---|---|
| **Your laptop** | `fleet_manager/fleet_manager.py` + an MQTT broker (`mosquitto`) | No | No |
| **Each Jetson** (one per drone) | `drone_node/main.py` | Yes | Yes — its own, only |

The laptop is pure coordination: it hands out drone IDs, pushes each
drone's mission file, and prints a live telemetry dashboard. It never
opens a MAVSDK/MAVLink connection. Every bit of flight logic — connecting
to the Pixhawk, arming, navigating, spraying, failsafes — runs on the
Jetson, and only that Jetson's own drone. There is no process anywhere
that holds multiple drones' flight connections at once; each `drone_node`
is a fully independent program, and the laptop + MQTT broker are the only
thing standing between "one drone flying alone" and "a swarm."

### MQTT pub/sub — how the pieces actually talk

MQTT is the only thing connecting the laptop to the Jetsons, and
Jetsons to each other. There's no other network call anywhere in this
system (no REST API, no shared database, no direct socket between
drones). A `mosquitto` broker runs once, on the laptop; everyone else
(`fleet_manager.py` and every `drone_node`) is just an MQTT client of it.
Drones never talk directly to each other — all messages pass through the
broker, keyed by topic name.

Three topics, defined in `drone_node/swarm/mqtt_link.py` and mirrored in
`fleet_manager/fleet_manager.py`:

| Topic | Published by | Subscribed by | Payload | Purpose |
|---|---|---|---|---|
| `swarm/register/request` | `drone_node` (`SwarmLink.register()`) | `fleet_manager` | `{"hardware_id": "jetson-01"}` | "I'm this piece of hardware — who am I and what's my mission?" |
| `swarm/register/ack/<hardware_id>` | `fleet_manager` (`_handle_register`) | the one `drone_node` with that `hardware_id` | `{"drone_id": "drone-01", "mission_xml": "<...>"}` | The answer: your assigned ID + the full mission file contents |
| `swarm/telemetry/<drone_id>` | `drone_node` (`SwarmLink.publish_telemetry()`, called every ~2s from `DroneAgent._publish_telemetry_loop`) | every other `drone_node` (via `swarm/telemetry/+` wildcard) **and** `fleet_manager` (dashboard) | `{"lat", "lon", "alt_m", "battery_pct", "state", "timestamp"}` | Live position/state broadcast — this is the entire swarm-awareness mechanism |

Two request/response flows ride on these topics:

1. **Registration (once, at startup)** — `drone_node` publishes a
   `register/request` with its `hardware_id`. `fleet_manager` looks that
   `hardware_id` up in `registry.json` (assigning a fresh `drone-NN` if
   it's never been seen before), reads `missions/<drone_id>.xml` off
   disk, and publishes it back on the drone's own per-hardware-id ack
   topic so only that drone receives it. The Jetson caches the mission
   XML locally (`missions_cache/`) and uses it for the rest of the flight
   — fleet_manager is not needed again after this point.
2. **Telemetry (continuously, while flying)** — every drone publishes its
   own state to `swarm/telemetry/<drone_id>` roughly every 2 seconds.
   Every `drone_node` subscribes to the wildcard `swarm/telemetry/+`, so
   it receives every *other* drone's telemetry too (it ignores its own
   echo) and feeds it into its local `SwarmState` — this is what lets a
   plugin ask "is any other drone critically low on battery?" without any
   central brain deciding anything. `fleet_manager` subscribes to the same
   wildcard purely to render the dashboard table; it doesn't act on it.

Each piece degrades independently: if the broker is unreachable, a
`drone_node` just never gets a registration ack, falls back to its
locally configured `fallback_mission`, and flies standalone with an empty
`SwarmState` (zero peers, no error). If `fleet_manager`/the broker isn't
running at all, nothing on the Jetson even notices until the registration
timeout — single-drone operation requires none of this infrastructure.

### File-by-file

```
swarm_system/
├── fleet_manager/                      # ───── LAPTOP ─────
│   ├── fleet_manager.py                  MQTT client: answers register/request with a
│   │                                      drone_id + mission XML, and prints a live
│   │                                      telemetry table from swarm/telemetry/+.
│   │                                      No MAVSDK, no Pixhawk, never flies anything.
│   ├── missions/drone-01.xml, ...        One mission file per drone_id, authored by hand.
│   ├── registry.json                     Auto-created/updated: hardware_id -> drone_id.
│   └── requirements.txt                  Just paho-mqtt — deliberately lightweight.
│
├── drone_node/                         # ───── JETSON (copy this whole folder per drone) ─────
│   ├── main.py                           Entry point. Loads drone_config.yaml, asks
│   │                                      fleet_manager who it is (or uses --mission to
│   │                                      skip that), parses the mission, wires up
│   │                                      DroneAgent + SwarmLink + SwarmState, flies.
│   ├── config/
│   │   └── drone_config.yaml             Per-Jetson hardware facts that never travel
│   │                                      over the network: hardware_id, broker IP,
│   │                                      serial port / SITL connection string,
│   │                                      mavsdk_server_port, fallback_mission.
│   ├── core/                             Mission data model + parsing + scheduling —
│   │   │                                  no MQTT, no swarm awareness, pure mission logic.
│   │   ├── mission_plan.py               Dataclasses: Waypoint, DroneConfig, SafetyConfig,
│   │   │                                  ScheduleConfig, MissionPlan (the parsed result).
│   │   ├── mission_parser.py             Reads a mission XML (validated against
│   │   │                                  mission_schema.xsd) into a MissionPlan.
│   │   ├── custom_code_runner.py         Dynamically loads/caches/executes the Python
│   │   │                                  plugin a waypoint's action="custom_code" points
│   │   │                                  at, passing it a drone_context dict.
│   │   └── scheduler.py                  APScheduler wrapper: fires the mission at
│   │                                      <StartDate> (or immediately with --run-now),
│   │                                      optionally repeats on a cron schedule.
│   ├── flight/                           Everything that actually talks to the Pixhawk.
│   │   ├── drone_agent.py                The core flight loop for THIS drone only:
│   │   │                                  connect -> preflight/GPS lock -> arm/takeoff ->
│   │   │                                  fly each waypoint -> run its action -> RTL/land.
│   │   │                                  Publishes telemetry every 2s (local bus + MQTT
│   │   │                                  callback) and checks the safety watchdog on
│   │   │                                  every position update.
│   │   ├── safety.py                     SafetyWatchdog (battery % + geofence radius
│   │   │                                  checks against SafetyConfig) and SprayController
│   │   │                                  (drives a real Jetson.GPIO pin, or logs a
│   │   │                                  simulation if GPIO isn't available).
│   │   └── telemetry_bus.py              In-process async pub/sub: DroneAgent publishes
│   │                                      TelemetryEvents here; anything inside the SAME
│   │                                      Python process can subscribe (per-drone or "*").
│   │                                      This is local-only — it is NOT the MQTT layer;
│   │                                      swarm/mqtt_link.py is what crosses the network.
│   ├── swarm/                             The ONLY code that talks MQTT.
│   │   ├── mqtt_link.py                  SwarmLink: the paho-mqtt client. Sends the
│   │   │                                  registration request, receives the ack, publishes
│   │   │                                  this drone's telemetry, and feeds every peer's
│   │   │                                  telemetry into SwarmState. Degrades to
│   │   │                                  "disconnected, no peers" if the broker is down.
│   │   └── swarm_state.py                SwarmState: in-memory dict of {drone_id ->
│   │                                      PeerState}, built purely from MQTT telemetry
│   │                                      messages. Exposes peers(), get(),
│   │                                      any_peer_critical_battery() — the hook custom
│   │                                      plugins use to react to what other drones are
│   │                                      doing. Zero peers is a completely normal state
│   │                                      (single-drone mode).
│   ├── plugins/
│   │   └── spray_pattern.py              Example custom_code plugin (zigzag/linear/
│   │                                      figure8/spiral patterns); receives drone_context
│   │                                      which includes drone_context["swarm_state"].
│   └── requirements.txt                  mavsdk (pinned <4.0.0), paho-mqtt, pyyaml,
│                                          apscheduler — everything flight-related.
│
└── missions/
    ├── mission_schema.xsd                 The XML schema every mission file must satisfy.
    └── standalone_mission.xml             Fallback mission for offline / single-drone
                                            testing — used when --mission is passed
                                            directly or when registration fails/times out.
```

### How the swarm actually works, end to end

1. **Boot.** Each Jetson runs `python main.py --config config/drone_config.yaml`
   independently — nothing coordinates the *start* of these processes; you
   just run one per drone, whenever that drone is powered on.
2. **Registration.** `SwarmLink` tries to connect to the broker at the
   laptop's IP (`drone_config.yaml: broker.host`). If it connects, it
   publishes `swarm/register/request` with this Jetson's `hardware_id`
   and waits (`register_timeout_sec`) for an ack on its own
   `swarm/register/ack/<hardware_id>` topic.
3. **ID + mission handout.** `fleet_manager`, already subscribed to
   `swarm/register/request`, resolves `hardware_id -> drone_id` (assigning
   `drone-01`, `drone-02`, ... the first time it sees a new hardware_id,
   persisted to `registry.json`), reads that drone's `missions/<drone_id>.xml`,
   and publishes the ack. The Jetson caches the mission XML to
   `missions_cache/<drone_id>.xml` and is now done needing the laptop.
4. **No broker / no ack / `--mission` passed.** Any of these fall back to
   `drone_config.yaml: fallback_mission` (or the explicit `--mission` path)
   — the drone flies standalone, `drone_id` becomes its own `hardware_id`,
   and `SwarmState` simply never gets any peer updates. This is the same
   code path as a full swarm, just with zero peers.
5. **Mission parse + flight.** `MissionParser` turns the XML into a
   `MissionPlan`; `main.py` pulls out just this `drone_id`'s waypoints and
   hands everything to `DroneAgent.execute_mission()`, which connects to
   this Jetson's own Pixhawk (via MAVSDK over serial or UDP), arms, takes
   off, flies each waypoint in order, runs that waypoint's action
   (spray on/off, a custom plugin, or nothing), then RTLs/lands — exactly
   as if it were the only drone that existed.
6. **Telemetry out.** While flying, `DroneAgent` publishes a
   `TelemetryEvent` roughly every 2 seconds, both to the local
   `TelemetryBus` (for anything inside this same process — currently just
   logging) and, via the `on_telemetry` callback wired to
   `SwarmLink.publish_telemetry`, to `swarm/telemetry/<drone_id>` on MQTT.
7. **Telemetry in / swarm awareness.** Every other `drone_node`'s
   `SwarmLink` is subscribed to `swarm/telemetry/+`, so it receives this
   drone's broadcast and updates its own `SwarmState` with a fresh
   `PeerState`. `DroneAgent` injects `swarm_state` into every
   `custom_code` plugin's `drone_context`, so a plugin running on *this*
   drone can query what *other* drones are doing right now (e.g.
   `swarm_state.any_peer_critical_battery(20)`) without any message having
   to pass through a central decision-maker — the "swarm" is just N
   independent agents each listening to the same broadcast channel.
8. **The laptop's view.** `fleet_manager` is subscribed to the same
   `swarm/telemetry/+` wildcard purely to print the live dashboard table —
   it's a passive observer of the swarm, not a controller of it. It also
   never expires stale rows, so a drone that already landed and exited
   still shows its last reading forever (cosmetic only).

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
