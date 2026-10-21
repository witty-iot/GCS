"""
main.py — drone_node entry point. Runs ON THE JETSON, one instance per drone.

Flow
----
1. Load this Jetson's local config (hardware_id, broker address, serial
   port, and any local hardware like a spray pump).
2. Try to register with fleet_manager over MQTT: "I'm jetson-01, who am I
   and what's my mission?" If the broker is unreachable or nobody answers
   in time, fall back to a locally cached/provided mission file and keep
   flying standalone — a single drone with no network must still work.
3. Parse the mission XML, pull out just this drone's own (nested) waypoints.
4. Build local hardware controllers (e.g. SprayController) from this
   Jetson's own config — never from the mission — and hand them to the
   flight agent as "extra_context," injected into every action plugin.
5. Fly the mission. While flying, publish telemetry to MQTT (if connected)
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
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

from core.action_runner import ActionRunner
from core.mission_parser import MissionParser, MissionParseError
from core.mission_plan import DroneConfig, MissionPlan
from core.scheduler import MissionScheduler
from flight.camera_feed import CameraFeed
from flight.drone_agent import DroneAgent
from flight.safety import SprayController
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
    """
    Thin adapter so MissionScheduler (which calls `coordinator.run()`) works
    unchanged for one drone.

    Guards against double-execution: the mission can now be triggered by
    TWO independent sources racing each other — fleet_manager's ground
    launch broadcast (the primary trigger your CEO asked for) and this
    drone's own local <StartDate> scheduler (the resilience fallback if
    the ground link is down at launch time). Whichever fires first
    actually flies; if the other fires afterward, this no-ops rather than
    flying the same mission twice.
    """

    def __init__(self, agent: DroneAgent, waypoints, runner: ActionRunner) -> None:
        self._agent = agent
        self._waypoints = waypoints
        self._runner = runner
        self._started = False
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        async with self._lock:
            if self._started:
                log.info(
                    "[%s] Mission already started via another trigger; ignoring duplicate launch.",
                    self._agent.config.id,
                )
                return
            self._started = True
        await self._agent.execute_mission(self._waypoints, self._runner)


async def _get_mission_xml_path(args: argparse.Namespace, cfg: dict, link: SwarmLink) -> tuple[str, str, Dict[str, Any]]:
    """
    Returns (mission_xml_path, drone_id, action_meta).

    action_meta is the per-action-code params dict fleet_manager bundled
    into the registration ack (see fleet_manager.py's _collect_action_meta)
    — the ground's current copy of every referenced action's meta.json,
    delivered alongside the mission itself rather than assumed to already
    be correct on this Jetson's local disk. Empty ({}) whenever there was
    no registration (--mission given, or registration failed) — in that
    case ActionRunner falls back to reading each plugin's local sidecar
    meta.json file directly, same as before this existed.

    Tries registration first (unless --mission was given, which skips the
    network entirely). Falls back to the local mission file on any failure.
    """
    if args.mission:
        log.info("--mission given; skipping registration, using %s directly.", args.mission)
        return args.mission, cfg["hardware_id"], {}

    ack = await link.register(timeout_sec=cfg["broker"]["register_timeout_sec"])
    if ack is not None:
        cache_path = Path("missions_cache") / f"{ack['drone_id']}.xml"
        cache_path.parent.mkdir(exist_ok=True)
        cache_path.write_text(ack["mission_xml"], encoding="utf-8")

        action_meta: Dict[str, Any] = ack.get("action_meta", {})
        meta_cache_path = Path("missions_cache") / f"{ack['drone_id']}.meta.json"
        meta_cache_path.write_text(json.dumps(action_meta), encoding="utf-8")

        log.info(
            "Received mission from fleet_manager for drone_id=%s -> cached at %s "
            "(action metadata for: %s)",
            ack["drone_id"], cache_path, ", ".join(sorted(action_meta)) or "none",
        )
        return str(cache_path), ack["drone_id"], action_meta

    fallback = cfg.get("fallback_mission")
    if not fallback:
        log.critical("No registration ack and no fallback_mission configured. Cannot proceed.")
        sys.exit(1)
    log.warning("Registration failed/unavailable. Falling back to local mission: %s", fallback)
    return fallback, cfg["hardware_id"], {}


def _resolve_own_drone_config(plan: MissionPlan, drone_id: str) -> DroneConfig:
    """
    Pull this drone's mission-relevant config (altitude, speed, its own
    waypoints) out of the parsed mission plan. Nothing hardware-specific
    lives on this object any more — there's no connection string or GPIO
    config to override here, because the mission never carried any.
    """
    try:
        return plan.drone_by_id(drone_id)
    except KeyError:
        log.critical(
            "Mission plan has no <Drone id='%s'> entry. "
            "Every drone this mission targets must have a matching Drone block.",
            drone_id,
        )
        sys.exit(1)


def _build_extra_context(cfg: dict) -> Dict[str, Any]:
    """
    Build this Jetson's local hardware controllers from its own config,
    never from the mission. Returns a dict merged into every action
    plugin's drone_context (e.g. {"spray": SprayController(...)}).
    Empty if this Jetson has no hardware section configured — action
    plugins that need it (spray_on.py/spray_off.py) no-op gracefully.
    """
    extra: Dict[str, Any] = {}
    hardware = cfg.get("hardware") or {}

    spray_cfg = hardware.get("spray")
    if spray_cfg:
        extra["spray"] = SprayController(
            gpio_pin=spray_cfg["gpio_pin"],
            pump_on_delay_ms=spray_cfg["pump_on_delay_ms"],
            pump_off_delay_ms=spray_cfg["pump_off_delay_ms"],
        )
        log.info("Local hardware: spray controller configured on GPIO %d.", spray_cfg["gpio_pin"])

    camera_cfg = hardware.get("camera")
    if camera_cfg:
        extra["camera"] = CameraFeed(source=camera_cfg.get("source", 0))
        log.info("Local hardware: camera feed configured on source %s.", camera_cfg.get("source", 0))

    return extra


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

    mission_path, drone_id, action_meta = await _get_mission_xml_path(args, cfg, link)
    if link.drone_id is None:
        link.drone_id = drone_id  # standalone fallback: use our own id as the telemetry topic name

    try:
        plan = MissionParser.parse(mission_path)
    except (FileNotFoundError, MissionParseError) as exc:
        log.critical("Mission parse failed: %s", exc)
        sys.exit(1)

    own_cfg = _resolve_own_drone_config(plan, drone_id)
    waypoints = own_cfg.waypoints
    if not waypoints:
        log.critical("No waypoints assigned to drone_id='%s' in this mission.", drone_id)
        sys.exit(1)

    if args.simulate and not cfg["drone"]["connection_string"].startswith("udp"):
        log.warning(
            "--simulate was passed but drone.connection_string is not a udp:// "
            "address. Each drone's SITL instance uses a different port "
            "(14540, 14541, ...) — set it explicitly per-Jetson in drone_config.yaml."
        )

    bus = TelemetryBus()
    runner = ActionRunner()
    if action_meta:
        runner.preload_meta(action_meta)
        log.info("Preloaded action metadata delivered with the mission for: %s", ", ".join(sorted(action_meta)))
    agent = DroneAgent(
        config=own_cfg,
        connection_string=cfg["drone"]["connection_string"],
        safety=plan.safety,
        bus=bus,
        swarm_state=swarm_state,
        mavsdk_server_port=cfg["drone"].get("mavsdk_server_port", 50051),
        on_telemetry=link.publish_telemetry,
        extra_context=_build_extra_context(cfg),
    )

    log.info(
        "Ready: drone_id=%s mission='%s' waypoints=%d peers_visible=%d",
        drone_id, plan.name, len(waypoints), len(swarm_state.peers()),
    )

    if args.run_now:
        await agent.execute_mission(waypoints, runner)
    else:
        # Two independent triggers race to start the mission:
        #   1. fleet_manager's ground launch broadcast (primary — the
        #      scheduling authority now lives on the ground, not here).
        #   2. This drone's own local <StartDate> scheduler (fallback —
        #      fires if the ground link is unreachable right at launch
        #      time, so a disconnected drone still isn't stranded).
        # _SingleDroneRunner guards against both firing.
        runner_wrapper = _SingleDroneRunner(agent, waypoints, runner)

        scheduler = MissionScheduler(runner_wrapper)
        scheduler.schedule_mission(plan)
        scheduler.start()

        async def _ground_launch_watcher() -> None:
            await link.wait_for_launch()
            log.info("[%s] Ground launch signal received — executing now.", drone_id)
            await runner_wrapper.run()

        ground_task = asyncio.create_task(_ground_launch_watcher())

        log.info(
            "Armed: waiting for ground launch signal (primary) or local <StartDate> %s (fallback). Ctrl-C to abort.",
            plan.schedule.start_date,
        )
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            scheduler.stop()
            ground_task.cancel()

    link.close()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("Interrupted by user.")


if __name__ == "__main__":
    main()
