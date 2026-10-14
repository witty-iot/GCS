"""
main.py — drone_node entry point. Runs ON THE JETSON, one instance per drone.

Flow
----
1. Load this Jetson's local config (hardware_id, broker address, serial port).
2. Try to register with fleet_manager over MQTT: "I'm jetson-01, who am I
   and what's my mission?" If the broker is unreachable or nobody answers
   in time, fall back to a locally cached/provided mission file and keep
   flying standalone — a single drone with no network must still work.
3. Parse the mission XML, pull out just this drone's waypoints.
4. Fly the mission. While flying, publish telemetry to MQTT (if connected)
   so every other drone's swarm_state sees this one, and this one sees them.

Usage
-----
  python main.py --config config/drone_config.yaml
  python main.py --config config/drone_config.yaml --run-now
  python main.py --config config/drone_config.yaml --simulate --run-now
  python main.py --config config/drone_config.yaml --mission ../missions/standalone_mission.xml --run-now
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml

from core.custom_code_runner import CustomCodeRunner
from core.mission_parser import MissionParser, MissionParseError
from core.mission_plan import DroneConfig, MissionPlan
from core.scheduler import MissionScheduler
from flight.drone_agent import DroneAgent
from flight.telemetry_bus import TelemetryBus
from swarm.mqtt_link import SwarmLink
from swarm.swarm_state import SwarmState

log = logging.getLogger("main")


def _setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-20s %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.INFO)
    stream.setFormatter(fmt)

    file_handler = logging.FileHandler(log_dir / "drone_node.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(stream)
    root.addHandler(file_handler)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Single-drone swarm-aware mission runner.")
    p.add_argument("--config", default="config/drone_config.yaml", help="Path to drone_config.yaml")
    p.add_argument("--mission", default=None, help="Override: run this local mission XML directly, skip registration entirely")
    p.add_argument("--run-now", action="store_true", help="Ignore <StartDate>, fly immediately")
    p.add_argument("--simulate", action="store_true", help="Warn if connection_string in drone_config.yaml isn't a udp:// address (SITL)")
    return p.parse_args()


class _SingleDroneRunner:
    """Thin adapter so MissionScheduler (which calls `coordinator.run()`) works unchanged for one drone."""

    def __init__(self, agent: DroneAgent, waypoints, runner: CustomCodeRunner) -> None:
        self._agent = agent
        self._waypoints = waypoints
        self._runner = runner

    async def run(self) -> None:
        await self._agent.execute_mission(self._waypoints, self._runner)


async def _get_mission_xml_path(args: argparse.Namespace, cfg: dict, link: SwarmLink) -> tuple[str, str]:
    """
    Returns (mission_xml_path, drone_id).

    Tries registration first (unless --mission was given, which skips the
    network entirely). Falls back to the local mission file on any failure.
    """
    if args.mission:
        log.info("--mission given; skipping registration, using %s directly.", args.mission)
        return args.mission, cfg["hardware_id"]

    ack = await link.register(timeout_sec=cfg["broker"]["register_timeout_sec"])
    if ack is not None:
        cache_path = Path("missions_cache") / f"{ack['drone_id']}.xml"
        cache_path.parent.mkdir(exist_ok=True)
        cache_path.write_text(ack["mission_xml"], encoding="utf-8")
        log.info("Received mission from fleet_manager for drone_id=%s -> cached at %s", ack["drone_id"], cache_path)
        return str(cache_path), ack["drone_id"]

    fallback = cfg.get("fallback_mission")
    if not fallback:
        log.critical("No registration ack and no fallback_mission configured. Cannot proceed.")
        sys.exit(1)
    log.warning("Registration failed/unavailable. Falling back to local mission: %s", fallback)
    return fallback, cfg["hardware_id"]


def _resolve_own_drone_config(plan: MissionPlan, drone_id: str, cfg: dict, simulate: bool) -> DroneConfig:
    """
    Pull this drone's config out of the parsed mission plan, but always
    override the connection string with the LOCAL config — a shared mission
    file should never dictate which serial port a specific Jetson uses.
    """
    try:
        drone_cfg = plan.drone_by_id(drone_id)
    except KeyError:
        log.critical(
            "Mission plan has no <Drone id='%s'> entry. "
            "Every drone this mission targets must have a matching Drone block.",
            drone_id,
        )
        sys.exit(1)

    if simulate and not cfg["drone"]["connection_string"].startswith("udp"):
        log.warning(
            "--simulate was passed but drone.connection_string is not a udp:// "
            "address. Each drone's SITL instance uses a different port "
            "(14540, 14541, ...) — set it explicitly per-Jetson in drone_config.yaml "
            "rather than relying on a single hardcoded port for every drone."
        )
    drone_cfg.connection_string = cfg["drone"]["connection_string"]
    return drone_cfg


async def _amain() -> None:
    args = _parse_args()
    _setup_logging()

    cfg_path = Path(args.config)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    log.info("Loaded config: hardware_id=%s", cfg["hardware_id"])

    swarm_state = SwarmState()
    link = SwarmLink(
        broker_host=cfg["broker"]["host"],
        broker_port=cfg["broker"]["port"],
        hardware_id=cfg["hardware_id"],
        swarm_state=swarm_state,
        connect_timeout_sec=cfg["broker"]["connect_timeout_sec"],
    )

    mission_path, drone_id = await _get_mission_xml_path(args, cfg, link)
    if link.drone_id is None:
        link.drone_id = drone_id  # standalone fallback: use our own id as the telemetry topic name

    try:
        plan = MissionParser.parse(mission_path)
    except (FileNotFoundError, MissionParseError) as exc:
        log.critical("Mission parse failed: %s", exc)
        sys.exit(1)

    own_cfg = _resolve_own_drone_config(plan, drone_id, cfg, args.simulate)
    waypoints = plan.waypoints_for(drone_id)
    if not waypoints:
        log.critical("No waypoints assigned to drone_id='%s' in this mission.", drone_id)
        sys.exit(1)

    bus = TelemetryBus()
    runner = CustomCodeRunner()
    agent = DroneAgent(
        config=own_cfg,
        safety=plan.safety,
        bus=bus,
        swarm_state=swarm_state,
        mavsdk_server_port=cfg["drone"].get("mavsdk_server_port", 50051),
        on_telemetry=link.publish_telemetry,
    )

    log.info(
        "Ready: drone_id=%s mission='%s' waypoints=%d peers_visible=%d",
        drone_id, plan.name, len(waypoints), len(swarm_state.peers()),
    )

    if args.run_now:
        await agent.execute_mission(waypoints, runner)
    else:
        runner_wrapper = _SingleDroneRunner(agent, waypoints, runner)
        scheduler = MissionScheduler(runner_wrapper)
        scheduler.schedule_mission(plan)
        scheduler.start()
        log.info("Scheduler armed. Waiting for start time (Ctrl-C to abort).")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            scheduler.stop()

    link.close()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("Interrupted by user.")


if __name__ == "__main__":
    main()
