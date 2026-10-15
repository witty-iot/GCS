"""
flight/drone_agent.py
----------------------
One drone, one process, one Jetson.

v2 changes (generic action model)
-----------------------------------
* No more spray-specific code in the flight agent at all. There is no
  ``SPRAY_ON``/``SPRAY_OFF`` branch, no ``self._spray`` member tied to
  mission data. Every waypoint action — spraying included — runs through
  the same generic ``ActionRunner``, resolved by action code to a plugin.
* Hardware controllers (e.g. a spray pump) are handed in from ``main.py``
  as an ``extra_context`` dict, built from each Jetson's *local* config,
  never from the mission. They're injected into every action plugin's
  ``drone_context`` under whatever key the Jetson owner chose (e.g.
  ``"spray"``), and on mission abort/error, any of them that implements
  an ``emergency_stop()`` method gets it called — generically, via
  duck-typing, without the flight agent knowing what kind of hardware
  it's shutting down.
* ``connection_string`` is no longer part of the mission-derived
  ``DroneConfig`` — it's always local-only, passed in as its own
  constructor argument.
* Per-waypoint ``Speed`` is honored via MAVSDK's
  ``action.set_current_speed()`` before each ``goto_location()`` call,
  falling back to the drone's ``default_speed_ms`` when a waypoint
  doesn't specify one.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mavsdk import System
from mavsdk.action import ActionError

from core.action_runner import ActionRunner
from core.mission_plan import DroneConfig, SafetyConfig, Waypoint
from flight.safety import SafetyWatchdog, haversine_m
from flight.telemetry_bus import DroneState, TelemetryBus, TelemetryEvent
from swarm.swarm_state import SwarmState

log = logging.getLogger(__name__)

WAYPOINT_ACCEPTANCE_RADIUS_M: float = 3.0
TELEMETRY_PUBLISH_INTERVAL_SEC: float = 2.0
ALTITUDE_ACCEPTANCE_M: float = 0.5
POSITION_POLL_INTERVAL_SEC: float = 0.5


class MissionAborted(Exception):
    """Raised internally when the safety watchdog trips mid-mission."""


class DroneAgent:
    def __init__(
        self,
        config: DroneConfig,
        connection_string: str,
        safety: SafetyConfig,
        bus: TelemetryBus,
        swarm_state: Optional[SwarmState] = None,
        on_telemetry=None,
        mavsdk_server_port: int = 50051,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.config = config
        self.connection_string = connection_string
        self.safety = safety
        self.bus = bus
        self.swarm_state = swarm_state or SwarmState()
        self._on_telemetry = on_telemetry  # optional callback(payload: dict) -> None, used to publish to MQTT
        self._mavsdk_server_port = mavsdk_server_port
        # Local-hardware objects (e.g. a spray controller), keyed by
        # whatever name the Jetson's own config chose. Merged into every
        # action plugin's drone_context. The flight agent never inspects
        # what's in here beyond duck-typing emergency_stop() on abort.
        self._extra_context: Dict[str, Any] = extra_context or {}

        self._state: DroneState = DroneState.IDLE
        self._system: Optional[System] = None
        self._telemetry_task: Optional[asyncio.Task] = None
        self._watchdog: Optional[SafetyWatchdog] = None

        self._last_lat: float = 0.0
        self._last_lon: float = 0.0
        self._last_alt: float = 0.0
        self._last_battery: float = 100.0

        log.info("DroneAgent initialised for '%s' (%s).", config.name, config.id)

    @property
    def state(self) -> DroneState:
        return self._state

    def _set_state(self, new_state: DroneState) -> None:
        if new_state != self._state:
            log.info("[%s] State: %s -> %s", self.config.id, self._state.value, new_state.value)
            self._state = new_state

    # ------------------------------------------------------------------
    # Connection & preflight
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        log.info(
            "[%s] Connecting to %s (mavsdk_server port %d) ...",
            self.config.id, self.connection_string, self._mavsdk_server_port,
        )
        self._system = System(port=self._mavsdk_server_port)
        await self._system.connect(system_address=self.connection_string)

        async for state in self._system.core.connection_state():
            if state.is_connected:
                log.info("[%s] Connected.", self.config.id)
                break

    async def preflight(self) -> None:
        if self._system is None:
            raise RuntimeError(f"[{self.config.id}] connect() must be called before preflight().")

        self._set_state(DroneState.PREFLIGHT)
        log.info("[%s] Waiting for GPS lock ...", self.config.id)

        home_lat = home_lon = 0.0
        async for health in self._system.telemetry.health():
            if health.is_global_position_ok and health.is_home_position_ok:
                log.info("[%s] GPS lock acquired.", self.config.id)
                break

        async for pos in self._system.telemetry.position():
            home_lat, home_lon = pos.latitude_deg, pos.longitude_deg
            break

        self._watchdog = SafetyWatchdog(self.safety, home_lat, home_lon)
        log.info("[%s] Home position set: (%.6f, %.6f).", self.config.id, home_lat, home_lon)

    # ------------------------------------------------------------------
    # Arm & takeoff
    # ------------------------------------------------------------------

    async def arm_and_takeoff(self) -> None:
        if self._system is None:
            raise RuntimeError(f"[{self.config.id}] Not connected.")

        log.info("[%s] Arming ...", self.config.id)
        await self._system.action.arm()

        target_alt = self.config.takeoff_altitude_m
        log.info("[%s] Taking off to %.1f m ...", self.config.id, target_alt)
        await self._system.action.set_takeoff_altitude(target_alt)
        await self._system.action.takeoff()

        self._set_state(DroneState.FLYING)

        async for pos in self._system.telemetry.position():
            current_alt = pos.relative_altitude_m
            self._last_lat, self._last_lon, self._last_alt = pos.latitude_deg, pos.longitude_deg, current_alt
            if current_alt >= target_alt - ALTITUDE_ACCEPTANCE_M:
                log.info("[%s] Reached takeoff altitude %.1f m.", self.config.id, current_alt)
                break

    # ------------------------------------------------------------------
    # Waypoint navigation
    # ------------------------------------------------------------------

    async def fly_to(self, wp: Waypoint) -> None:
        if self._system is None:
            raise RuntimeError(f"[{self.config.id}] Not connected.")

        log.info(
            "[%s] Flying to WP %d lat=%.6f lon=%.6f alt=%.1f ('%s')",
            self.config.id, wp.id, wp.lat, wp.lon, wp.alt, wp.description,
        )

        target_speed = wp.speed if wp.speed is not None else self.config.default_speed_ms
        try:
            await self._system.action.set_current_speed(target_speed)
        except ActionError as exc:
            log.warning("[%s] Could not set speed to %.1f m/s: %s", self.config.id, target_speed, exc)

        await self._system.action.goto_location(
            latitude_deg=wp.lat, longitude_deg=wp.lon,
            absolute_altitude_m=wp.alt, yaw_deg=float("nan"),
        )

        async for pos in self._system.telemetry.position():
            self._last_lat, self._last_lon, self._last_alt = (
                pos.latitude_deg, pos.longitude_deg, pos.relative_altitude_m,
            )

            self._check_safety()  # may raise MissionAborted

            dist = haversine_m(pos.latitude_deg, pos.longitude_deg, wp.lat, wp.lon)
            if dist <= WAYPOINT_ACCEPTANCE_RADIUS_M:
                log.info("[%s] WP %d reached (%.1f m).", self.config.id, wp.id, dist)
                break

            await asyncio.sleep(POSITION_POLL_INTERVAL_SEC)

        if wp.loiter_sec > 0:
            await asyncio.sleep(wp.loiter_sec)

    def _check_safety(self) -> None:
        """Raise MissionAborted the moment the watchdog trips. Checked on every position update."""
        if self._watchdog is None:
            return
        if self._watchdog.check(self._last_lat, self._last_lon, self._last_battery):
            raise MissionAborted(self._watchdog.reason or "safety watchdog triggered")

    # ------------------------------------------------------------------
    # Action dispatch — fully generic, no action is special-cased
    # ------------------------------------------------------------------

    async def handle_actions(self, wp: Waypoint, runner: ActionRunner) -> None:
        """Run every action attached to this waypoint, in XML order."""
        if not wp.actions:
            log.debug("[%s] WP %d has no actions; nothing to run.", self.config.id, wp.id)
            return

        drone_context: Dict[str, Any] = {
            "drone_id": self.config.id,
            "position": {"lat": self._last_lat, "lon": self._last_lon, "alt": self._last_alt},
            "battery_pct": self._last_battery,
            "mavsdk_system": self._system,
            "swarm_state": self.swarm_state,
            **self._extra_context,  # local hardware controllers (e.g. "spray"), if any
        }
        for action in wp.actions:
            await runner.run(action, drone_context)

    # ------------------------------------------------------------------
    # Telemetry publish loop (local bus + optional MQTT callback)
    # ------------------------------------------------------------------

    async def _publish_telemetry_loop(self) -> None:
        if self._system is None:
            return

        try:
            while True:
                try:
                    async for batt in self._system.telemetry.battery():
                        self._last_battery = batt.remaining_percent
                        break
                except Exception as exc:
                    log.warning("[%s] Battery telemetry read failed: %s", self.config.id, exc)

                event = TelemetryEvent(
                    drone_id=self.config.id,
                    timestamp=datetime.now(tz=timezone.utc),
                    lat=self._last_lat, lon=self._last_lon, alt_m=self._last_alt,
                    battery_pct=self._last_battery, state=self._state,
                )
                await self.bus.publish(event)

                if self._on_telemetry is not None:
                    self._on_telemetry({
                        "lat": event.lat, "lon": event.lon, "alt_m": event.alt_m,
                        "battery_pct": event.battery_pct, "state": event.state.value,
                        "timestamp": event.timestamp.isoformat(),
                    })

                await asyncio.sleep(TELEMETRY_PUBLISH_INTERVAL_SEC)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Safety helpers
    # ------------------------------------------------------------------

    async def _emergency_shutdown(self) -> None:
        """
        Generic shutdown on mission abort/error — calls emergency_stop()
        on any hardware object in extra_context that defines one. The
        flight agent has no idea what kind of hardware it's shutting
        down; it's purely duck-typed.
        """
        for name, obj in self._extra_context.items():
            stop_fn = getattr(obj, "emergency_stop", None)
            if callable(stop_fn):
                try:
                    await asyncio.get_event_loop().run_in_executor(None, stop_fn)
                    log.info("[%s] Emergency stop issued for '%s'.", self.config.id, name)
                except Exception as exc:
                    log.error("[%s] Emergency stop failed for '%s': %s", self.config.id, name, exc)

    async def _attempt_rtl(self) -> None:
        if self._system is None:
            return
        try:
            log.info("[%s] Commanding RTL.", self.config.id)
            await self._system.action.return_to_launch()
            self._set_state(DroneState.RTL)
        except Exception as exc:
            log.error("[%s] RTL command failed: %s", self.config.id, exc)
            self._set_state(DroneState.ERROR)

    # ------------------------------------------------------------------
    # Full mission lifecycle
    # ------------------------------------------------------------------

    async def execute_mission(self, waypoints: List[Waypoint], runner: ActionRunner) -> None:
        log.info("[%s] Starting mission: %d waypoints.", self.config.id, len(waypoints))

        try:
            await self.connect()

            self._telemetry_task = asyncio.create_task(
                self._publish_telemetry_loop(), name=f"telemetry_{self.config.id}",
            )

            await self.preflight()
            await self.arm_and_takeoff()

            for wp in waypoints:
                await self.fly_to(wp)
                await self.handle_actions(wp, runner)

            await self._emergency_shutdown()  # safety net: ensure all hardware is off before RTL
            await self._attempt_rtl()

            log.info("[%s] Waiting for landing ...", self.config.id)
            async for landed_state in self._system.telemetry.landed_state():  # type: ignore[union-attr]
                if "ON_GROUND" in str(landed_state):
                    log.info("[%s] Landed.", self.config.id)
                    self._set_state(DroneState.LANDED)
                    break

            try:
                await self._system.action.disarm()  # type: ignore[union-attr]
                log.info("[%s] Disarmed.", self.config.id)
            except ActionError as exc:
                log.warning("[%s] Disarm failed (may already be disarmed): %s", self.config.id, exc)

        except MissionAborted as exc:
            log.warning("[%s] Safety watchdog aborted mission: %s. RTL now.", self.config.id, exc)
            self._set_state(DroneState.ERROR)
            await self._emergency_shutdown()
            await self._attempt_rtl()

        except ActionError as exc:
            log.error("[%s] ActionError: %s. Emergency shutdown + RTL.", self.config.id, exc, exc_info=True)
            self._set_state(DroneState.ERROR)
            await self._emergency_shutdown()
            await self._attempt_rtl()

        except Exception as exc:
            log.error("[%s] Unexpected exception: %s. Attempting RTL.", self.config.id, exc, exc_info=True)
            self._set_state(DroneState.ERROR)
            await self._emergency_shutdown()
            await self._attempt_rtl()

        finally:
            if self._telemetry_task is not None and not self._telemetry_task.done():
                self._telemetry_task.cancel()
                try:
                    await self._telemetry_task
                except asyncio.CancelledError:
                    pass
            log.info("[%s] Mission cleanup complete.", self.config.id)
