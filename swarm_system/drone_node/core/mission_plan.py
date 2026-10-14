"""
core/mission_plan.py
--------------------
Data model for a parsed drone mission. All fields are
immutable value objects — no business logic here.

Classes
-------
WaypointAction   : Enum of supported waypoint-level actions.
CustomCodeConfig : Configuration for a dynamic plugin call at a waypoint.
Waypoint         : A single geographic target with metadata and action.
SprayConfig      : GPIO and pump-timing settings for the sprayer hardware.
DroneConfig      : Full configuration for one drone in the fleet.
SafetyConfig     : Operational limits and failsafe thresholds.
ScheduleConfig   : Mission start-time and optional repeat schedule.
MissionPlan      : Top-level mission container returned by the parser.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class WaypointAction(str, Enum):
    """Supported actions a drone can perform upon reaching a waypoint."""

    SPRAY_ON    = "spray_on"
    SPRAY_OFF   = "spray_off"
    NONE        = "none"
    CUSTOM_CODE = "custom_code"


# ---------------------------------------------------------------------------
# Leaf data-classes (no dependencies on other dataclasses in this module)
# ---------------------------------------------------------------------------

@dataclass
class CustomCodeConfig:
    """
    Describes a dynamic plugin call to be executed at a waypoint.

    Attributes
    ----------
    module_path   : File-system path (relative to project root) of the plugin.
    function_name : Name of the callable inside the module.
    args          : Keyword arguments forwarded to the plugin function.
    """

    module_path:   str
    function_name: str
    args:          Dict[str, Any] = field(default_factory=dict)


@dataclass
class Waypoint:
    """
    A single geographic target with an associated action.

    Attributes
    ----------
    id          : Unique integer identifier (from XML attribute).
    drone_id    : ID of the drone responsible for this waypoint.
    lat         : Latitude in decimal degrees (WGS-84).
    lon         : Longitude in decimal degrees (WGS-84).
    alt         : Altitude in metres (interpretation depends on firmware mode).
    action      : One of the ``WaypointAction`` enum values.
    loiter_sec  : Seconds to hover at the waypoint before proceeding.
    description : Free-text description for GCS display / logging.
    custom_code : Present and non-None only when action == CUSTOM_CODE.
    """

    id:          int
    drone_id:    str
    lat:         float
    lon:         float
    alt:         float
    action:      WaypointAction
    loiter_sec:  float                    = 0.0
    description: str                      = ""
    custom_code: Optional[CustomCodeConfig] = None


@dataclass
class SprayConfig:
    """
    GPIO / hardware settings for the drone's spray system.

    Attributes
    ----------
    gpio_pin         : BCM GPIO pin number that drives the relay/pump.
    pump_on_delay_ms : Milliseconds after GPIO HIGH before spray begins.
    pump_off_delay_ms: Milliseconds after GPIO LOW before spray stops.
    """

    gpio_pin:          int
    pump_on_delay_ms:  int
    pump_off_delay_ms: int


@dataclass
class DroneConfig:
    """
    Full configuration for one drone participating in the mission.

    Attributes
    ----------
    id                : Unique drone identifier (matches waypoint drone_id).
    name              : Human-readable name (e.g. "Alpha").
    connection_string : MAVSDK connection string (serial or UDP).
    takeoff_altitude_m: Target altitude reached during arm-and-takeoff.
    default_speed_ms  : Cruise speed used for goto_location calls (m/s).
    spray             : Hardware settings for the spray subsystem.
    """

    id:                 str
    name:               str
    connection_string:  str
    takeoff_altitude_m: float
    default_speed_ms:   float
    spray:              SprayConfig


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

    Produced by ``MissionParser.parse()`` and consumed by
    ``SwarmCoordinator`` and ``MissionScheduler``.

    Attributes
    ----------
    id                : Mission identifier string (e.g. ``"ALPHA-001"``).
    name              : Human-readable mission name.
    version           : Schema/mission version string.
    schedule          : Start-time and optional repeat configuration.
    drones            : Ordered list of drone configurations.
    waypoints         : Full ordered list of waypoints (all drones).
    safety            : Fleet-wide safety limits.
    rtl_after_mission : When True each drone RTLs after its last waypoint.
    """

    id:                str
    name:              str
    version:           str
    schedule:          ScheduleConfig
    drones:            List[DroneConfig]
    waypoints:         List[Waypoint]
    safety:            SafetyConfig
    rtl_after_mission: bool = True

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def waypoints_for(self, drone_id: str) -> List[Waypoint]:
        """
        Return only the waypoints assigned to the given drone.

        Parameters
        ----------
        drone_id : The drone identifier to filter by.

        Returns
        -------
        List[Waypoint]
            Ordered subset of ``self.waypoints`` where
            ``waypoint.drone_id == drone_id``.
        """
        return [w for w in self.waypoints if w.drone_id == drone_id]

    def drone_by_id(self, drone_id: str) -> DroneConfig:
        """
        Return ``DroneConfig`` for the given identifier.

        Parameters
        ----------
        drone_id : Unique drone identifier to look up.

        Returns
        -------
        DroneConfig
            The matching drone configuration.

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

