"""
fleet_manager.py — runs ON YOUR LAPTOP (not the Jetson).

Three jobs:
1. Registry — when a Jetson asks "who am I?" (identified by its stable
   hardware_id), look up or assign a drone_id, and hand back the mission
   XML that drone_id should fly. Mapping persists in registry.json so
   drone IDs survive restarts.
2. Mission distribution — mission XML files live in missions/<drone_id>.xml
   on the laptop; this script reads them and pushes the contents to the
   requesting drone over MQTT (no central server push needed on the Jetson
   side — the Jetson pulls once at startup).
3. Fleet dashboard — subscribes to every drone's telemetry topic and
   prints a live status table, exactly like drone_brain's status monitor,
   but now fed by real distributed drones instead of one process's
   internal agents.

This script does NOT fly anything and does NOT need MAVSDK. It only needs
an MQTT broker running (see README.md for `mosquitto` setup) and network
reachability to every drone's WiFi/mesh.

Usage
-----
  python fleet_manager.py --broker-host 0.0.0.0 --broker-port 1883
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict

import paho.mqtt.client as mqtt

log = logging.getLogger("fleet_manager")

REGISTRY_FILE = Path(__file__).parent / "registry.json"
MISSIONS_DIR = Path(__file__).parent / "missions"

REGISTER_REQUEST_TOPIC = "swarm/register/request"
REGISTER_ACK_PREFIX = "swarm/register/ack"
TELEMETRY_WILDCARD = "swarm/telemetry/+"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


class Registry:
    """hardware_id -> drone_id, persisted to registry.json."""

    def __init__(self) -> None:
        self._map: Dict[str, str] = {}
        self._next_num = 1
        if REGISTRY_FILE.exists():
            self._map = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            nums = [int(v.split("-")[-1]) for v in self._map.values() if v.split("-")[-1].isdigit()]
            self._next_num = max(nums, default=0) + 1

    def _save(self) -> None:
        REGISTRY_FILE.write_text(json.dumps(self._map, indent=2), encoding="utf-8")

    def resolve(self, hardware_id: str) -> str:
        """Return the existing drone_id for this hardware, assigning a new one if unseen."""
        if hardware_id in self._map:
            return self._map[hardware_id]
        drone_id = f"drone-{self._next_num:02d}"
        self._next_num += 1
        self._map[hardware_id] = drone_id
        self._save()
        log.info("Registry: assigned new drone_id '%s' to hardware_id '%s'.", drone_id, hardware_id)
        return drone_id


def _load_mission_xml(drone_id: str) -> str | None:
    """
    Each drone_id has its own mission file: missions/<drone_id>.xml
    This is the full per-drone XML described in missions/mission_schema.xsd
    (a <Mission> with exactly one <Drone id="drone_id"> and that drone's
    own waypoints). Keeping one file per drone (rather than one shared
    swarm file) is deliberate — it's simpler to author/update one drone's
    plan without touching anyone else's, and it's what makes "one drone
    standalone" and "N drones" the same code path.
    """
    path = MISSIONS_DIR / f"{drone_id}.xml"
    if not path.exists():
        log.warning("No mission file found for %s at %s", drone_id, path)
        return None
    return path.read_text(encoding="utf-8")


class FleetManager:
    def __init__(self, broker_host: str, broker_port: int) -> None:
        self.registry = Registry()
        self._latest_telemetry: Dict[str, dict] = {}

        self._client = mqtt.Client(client_id="fleet_manager", clean_session=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(broker_host, broker_port, keepalive=30)

    def _on_connect(self, client, userdata, flags, rc) -> None:
        log.info("Connected to broker (rc=%s). Listening for drones ...", rc)
        client.subscribe(REGISTER_REQUEST_TOPIC)
        client.subscribe(TELEMETRY_WILDCARD)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            log.warning("Malformed message on %s: %s", msg.topic, exc)
            return

        if msg.topic == REGISTER_REQUEST_TOPIC:
            self._handle_register(payload)
        elif msg.topic.startswith("swarm/telemetry/"):
            drone_id = msg.topic.rsplit("/", 1)[-1]
            self._latest_telemetry[drone_id] = payload

    def _handle_register(self, payload: dict) -> None:
        hardware_id = payload.get("hardware_id")
        if not hardware_id:
            log.warning("Registration request missing hardware_id: %s", payload)
            return

        drone_id = self.registry.resolve(hardware_id)
        mission_xml = _load_mission_xml(drone_id)
        if mission_xml is None:
            log.error(
                "No mission available for %s (hardware_id=%s). "
                "Drop a missions/%s.xml file and the drone will retry registration.",
                drone_id, hardware_id, drone_id,
            )
            return

        ack = {"drone_id": drone_id, "mission_xml": mission_xml}
        self._client.publish(f"{REGISTER_ACK_PREFIX}/{hardware_id}", json.dumps(ack), qos=1)
        log.info("Registered %s -> %s, mission pushed.", hardware_id, drone_id)

    def run_dashboard_forever(self, interval_sec: float = 5.0) -> None:
        log.info("Fleet dashboard running. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(interval_sec)
                self._print_table()
        except KeyboardInterrupt:
            log.info("Stopped.")

    def _print_table(self) -> None:
        if not self._latest_telemetry:
            print("(no telemetry received yet)")
            return
        print(f"\n{'-'*70}\nFleet Status  [{time.strftime('%H:%M:%S')}]\n{'-'*70}")
        print(f"{'Drone':<12}{'State':<12}{'Lat':>10}{'Lon':>12}{'Alt(m)':>9}{'Batt%':>8}")
        for drone_id, t in sorted(self._latest_telemetry.items()):
            print(
                f"{drone_id:<12}{t.get('state',''):<12}{t.get('lat',0):>10.5f}"
                f"{t.get('lon',0):>12.5f}{t.get('alt_m',0):>9.1f}{t.get('battery_pct',0):>8.1f}"
            )
        print(f"{'-'*70}")


def main() -> None:
    _setup_logging()
    p = argparse.ArgumentParser(description="Laptop-side fleet manager: registry + mission push + dashboard.")
    p.add_argument("--broker-host", default="localhost")
    p.add_argument("--broker-port", type=int, default=1883)
    args = p.parse_args()

    MISSIONS_DIR.mkdir(exist_ok=True)
    fm = FleetManager(args.broker_host, args.broker_port)
    fm._client.loop_start()
    fm.run_dashboard_forever()
    fm._client.loop_stop()


if __name__ == "__main__":
    main()
