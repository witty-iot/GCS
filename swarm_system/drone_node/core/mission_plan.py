"""
core/mission_plan.py
--------------------
Data model for a parsed drone mission. All fields are
immutable value objects — no business logic here.

v2 schema changes (vs. v1)
---------------------------
* Waypoints now live nested inside each <Drone>, not as a flat sibling
  list linked by a drone_id attribute — a mission reads as "this drone
  does these things," not "here's every point, cross-reference the ID."
* Every waypoint action is a plain action code (string) resolved to a
  plugin at runtime by convention — there is no more built-in
  "spray_on"/"spray_off" special case. Spraying is just a plugin like
  any other; the schema has no opinion about what a drone's payload is.
* Hardware wiring (GPIO pins, pump timing, serial port) is gone from
  this file entirely — it was never mission data, it's a property of
  one physical Jetson's wiring, and now lives only in that Jetson's
  local drone_config.yaml, never transmitted over the network.
* Waypoints may carry an optional per-waypoint Speed, independent of
  the drone's default cruise speed.

Classes
-------
ActionConfig   : An action code to resolve and run at a waypoint; its
                 parameters live in a plugins/<code>.meta.json sidecar
                 file, not in the mission itself.
Waypoint       : A single geographic target with metadata and its actions.
DroneConfig    : One drone's mission-relevant config (altitude, speed, waypoints).
SafetyConfig   : Operational limits and failsafe thresholds.
ScheduleConfig : Mission start-time and optional repeat schedule.
MissionPlan    : Top-level mission container returned by the parser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


# ---------------------------------------------------------------------------
# Leaf data-classes (no dependencies on other dataclasses in this module)
# ---------------------------------------------------------------------------

@dataclass
class ActionConfig:
    """
    An action to run at a waypoint, resolved to a plugin at runtime.

    Attributes
    ----------
    code : Short identifier (e.g. "spray_on", "take_photo", "spray_pattern").
           Resolved by convention to ``plugins/<code>.py``'s ``run()``
           function — no action is special-cased anywhere in the system.
           Parameters are NOT carried here — they live in that plugin's
           own ``plugins/<code>.meta.json`` sidecar file, loaded by
           ``core/action_runner.py``. The mission only names the action.
    """

    code: str


@dataclass
class Waypoint:
    """
    A single geographic target with zero or more actions to run on arrival.

    Attributes
    ----------
    id          : Unique integer identifier (from XML attribute), unique
                  within its parent drone.
    lat         : Latitude in decimal degrees (WGS-84).
    lon         : Longitude in decimal degrees (WGS-84).
    alt         : Altitude in metres (interpretation depends on firmware mode).
    actions      : Actions to run on arrival, in XML order. Empty list means
                   "just fly here, do nothing."
    actions_mode : "sequential" (default, run one at a time, in order) or
                   "parallel" (launch all of this waypoint's actions at once).
    speed        : Optional per-waypoint cruise speed (m/s). Falls back to
                   the drone's default_speed_ms when not set.
    loiter_sec   : Seconds to hover at the waypoint before proceeding.
    description  : Free-text description for GCS display / logging.
    """

    id:           int
    lat:          float
    lon:          float
    alt:          float
    actions:      List[ActionConfig] = field(default_factory=list)
    actions_mode: str                = "sequential"
    speed:        Optional[float]    = None
    loiter_sec:   float              = 0.0
    description:  str                = ""


@dataclass
class DroneConfig:
    """
    One drone's mission-relevant configuration — flight parameters and its
    own waypoints. Deliberately contains nothing hardware-specific (no
    serial port, no GPIO pins) — that's local-only data living on each
    Jetson, never part of a mission that travels over the network.

    Attributes
    ----------
    id                : Unique drone identifier.
    name              : Human-readable name (e.g. "Alpha").
    takeoff_altitude_m: Target altitude reached during arm-and-takeoff.
    default_speed_ms  : Cruise speed used when a waypoint has no own Speed.
    waypoints         : This drone's own ordered list of waypoints.
    """

    id:                 str
    name:               str
    takeoff_altitude_m: float
    default_speed_ms:   float
    waypoints:          List[Waypoint] = field(default_factory=list)


@dataclass
class SafetyConfig:
    """
    Operational limits and failsafe thresholds applied across the fleet.

    Attributes
    ----------
    min_battery_percent   : RTL triggered when battery % falls below this value.
    max_hdop              : Maximum acceptable HDOP; above this GPS is poor.
    heartbeat_timeout_sec : Seconds without heartbeat before declaring drone lost.
    geofence_radius_m     : Max distance from home before geofence RTL fires.
    """

    min_battery_percent:    int
    max_hdop:               float
    heartbeat_timeout_sec:  int
    geofence_radius_m:      float


@dataclass
class ScheduleConfig:
    """
    Defines when the mission starts and whether it repeats.

    Attributes
    ----------
    start_date  : Absolute date-time for the first launch (timezone-aware
                  if parsed with pytz, or naive if parsed from ISO text).
    timezone    : IANA timezone string (e.g. ``"Asia/Kolkata"``).
    repeat_cron : Optional 5-field cron expression for recurring launches.
    """

    start_date:  datetime
    timezone:    str
    repeat_cron: Optional[str] = None


# ---------------------------------------------------------------------------
# Top-level mission container
# ---------------------------------------------------------------------------

@dataclass
class MissionPlan:
    """
    Top-level container for a parsed drone mission.

    Produced by ``MissionParser.parse()`` and consumed by the scheduler
    and each drone's flight agent.

    Attributes
    ----------
    id                : Mission identifier string (e.g. ``"ALPHA-001"``).
    name              : Human-readable mission name.
    version           : Schema/mission version string.
    schedule          : Start-time and optional repeat configuration.
    drones            : Ordered list of drone configurations, each owning
                        its own waypoints.
    safety            : Fleet-wide safety limits.
    rtl_after_mission : When True each drone RTLs after its last waypoint.
    """

    id:                str
    name:              str
    version:           str
    schedule:          ScheduleConfig
    drones:            List[DroneConfig]
    safety:            SafetyConfig
    rtl_after_mission: bool = True

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def drone_by_id(self, drone_id: str) -> DroneConfig:
        """
        Return ``DroneConfig`` for the given identifier.

        Raises
        ------
        KeyError
            If no drone with ``drone_id`` exists in this mission.
        """
        for d in self.drones:
            if d.id == drone_id:
                return d
        raise KeyError(
            f"No drone with id '{drone_id}' in mission '{self.id}'"
        )
