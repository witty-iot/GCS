"""
core/mission_parser.py
----------------------
Parses a swarm mission XML file (validated against mission_schema.xsd)
into a ``MissionPlan`` object ready for consumption by the scheduler
and each drone's flight agent.

The parser is intentionally strict: every required field that is absent
will raise ``MissionParseError`` with a clear, actionable message.

Usage
-----
>>> from core.mission_parser import MissionParser
>>> plan = MissionParser.parse("missions/swarm_mission_alpha.xml")
>>> print(plan.name, plan.schedule.start_date)
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Optional

from core.mission_plan import (
    ActionConfig,
    DroneConfig,
    MissionPlan,
    SafetyConfig,
    ScheduleConfig,
    Waypoint,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MissionParseError(Exception):
    """
    Raised when the mission XML is structurally invalid or missing a
    required field.  The message always identifies the offending XML
    path so that operators can fix the file quickly.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require(element: Optional[ET.Element], tag: str, context: str) -> ET.Element:
    if element is None:
        raise MissionParseError(
            f"Required element <{tag}> is missing inside <{context}>."
        )
    return element


def _require_text(element: ET.Element, tag: str, context: str) -> str:
    text = (element.text or "").strip()
    if not text:
        raise MissionParseError(
            f"Required element <{tag}> inside <{context}> is empty."
        )
    return text


def _find_text(element: ET.Element, tag: str, default: str = "") -> str:
    child = element.find(tag)
    if child is None or not child.text:
        return default
    return child.text.strip()


def _find_float(element: ET.Element, tag: str, context: str) -> float:
    child = _require(element.find(tag), tag, context)
    text = _require_text(child, tag, context)
    try:
        return float(text)
    except ValueError as exc:
        raise MissionParseError(
            f"<{tag}> inside <{context}> is not a valid number: '{text}'"
        ) from exc


def _find_int(element: ET.Element, tag: str, context: str) -> int:
    child = _require(element.find(tag), tag, context)
    text = _require_text(child, tag, context)
    try:
        return int(text)
    except ValueError as exc:
        raise MissionParseError(
            f"<{tag}> inside <{context}> is not a valid integer: '{text}'"
        ) from exc


def _find_optional_float(element: ET.Element, tag: str, context: str) -> Optional[float]:
    child = element.find(tag)
    if child is None or not (child.text or "").strip():
        return None
    text = child.text.strip()
    try:
        return float(text)
    except ValueError as exc:
        raise MissionParseError(
            f"<{tag}> inside <{context}> is not a valid number: '{text}'"
        ) from exc


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class MissionParser:
    """
    Converts a swarm mission XML file into a ``MissionPlan`` dataclass.

    Only static/class methods are exposed — instantiation is not needed.
    """

    @staticmethod
    def parse(xml_path: str) -> MissionPlan:
        log.info("Parsing mission file: %s", xml_path)

        tree = ET.parse(xml_path)
        root = tree.getroot()

        if root.tag != "Mission":
            raise MissionParseError(
                f"Root element must be <Mission>, got <{root.tag}>."
            )

        mission_id      = root.get("id")      or ""
        mission_name    = root.get("name")    or ""
        mission_version = root.get("version") or ""

        for attr, val in [("id", mission_id), ("name", mission_name), ("version", mission_version)]:
            if not val:
                raise MissionParseError(f"<Mission> is missing required attribute '{attr}'.")

        log.debug("Mission id=%s name=%s version=%s", mission_id, mission_name, mission_version)

        schedule = MissionParser._parse_schedule(
            _require(root.find("Schedule"), "Schedule", "Mission")
        )
        drones = MissionParser._parse_fleet(
            _require(root.find("Fleet"), "Fleet", "Mission")
        )
        safety = MissionParser._parse_safety(
            _require(root.find("Safety"), "Safety", "Mission")
        )

        rtl_elem = root.find("RtlAfterMission")
        rtl_after = True
        if rtl_elem is not None and rtl_elem.text:
            rtl_after = rtl_elem.text.strip().lower() == "true"

        plan = MissionPlan(
            id=mission_id,
            name=mission_name,
            version=mission_version,
            schedule=schedule,
            drones=drones,
            safety=safety,
            rtl_after_mission=rtl_after,
        )

        total_wps = sum(len(d.waypoints) for d in plan.drones)
        log.info(
            "Mission '%s' parsed successfully: %d drone(s), %d waypoint(s) total.",
            plan.name, len(plan.drones), total_wps,
        )
        return plan

    # ------------------------------------------------------------------
    # Section parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_schedule(elem: ET.Element) -> ScheduleConfig:
        start_text = _require_text(
            _require(elem.find("StartDate"), "StartDate", "Schedule"),
            "StartDate", "Schedule",
        )
        timezone = _require_text(
            _require(elem.find("TimeZone"), "TimeZone", "Schedule"),
            "TimeZone", "Schedule",
        )

        try:
            start_date = datetime.fromisoformat(start_text)
        except ValueError as exc:
            raise MissionParseError(
                f"<StartDate> value '{start_text}' is not valid ISO 8601."
            ) from exc

        repeat_cron_elem = elem.find("RepeatCron")
        repeat_cron: Optional[str] = None
        if repeat_cron_elem is not None and repeat_cron_elem.text:
            repeat_cron = repeat_cron_elem.text.strip() or None

        log.debug("Schedule: start=%s tz=%s cron=%s", start_date, timezone, repeat_cron)
        return ScheduleConfig(
            start_date=start_date,
            timezone=timezone,
            repeat_cron=repeat_cron,
        )

    @staticmethod
    def _parse_fleet(elem: ET.Element) -> List[DroneConfig]:
        """
        Parse the ``<Fleet>`` element into a list of ``DroneConfig`` objects,
        each carrying its own nested ``<Waypoints>``.
        """
        drone_elems = elem.findall("Drone")
        if not drone_elems:
            raise MissionParseError("<Fleet> contains no <Drone> elements.")

        drones: List[DroneConfig] = []
        for d in drone_elems:
            drone_id   = d.get("id")   or ""
            drone_name = d.get("name") or ""
            if not drone_id:
                raise MissionParseError("<Drone> element is missing required attribute 'id'.")

            ctx = f"Drone[{drone_id}]"
            t_alt = _find_float(d, "TakeoffAltitude", ctx)
            speed = _find_float(d, "DefaultSpeed", ctx)

            waypoints_elem = _require(d.find("Waypoints"), "Waypoints", ctx)
            waypoints = MissionParser._parse_waypoints(waypoints_elem, ctx)

            cfg = DroneConfig(
                id=drone_id,
                name=drone_name,
                takeoff_altitude_m=t_alt,
                default_speed_ms=speed,
                waypoints=waypoints,
            )
            log.debug("Parsed drone: %s (%s), %d waypoint(s)", drone_name, drone_id, len(waypoints))
            drones.append(cfg)

        return drones

    @staticmethod
    def _parse_waypoints(elem: ET.Element, drone_ctx: str) -> List[Waypoint]:
        """
        Parse a ``<Waypoints>`` element (nested inside one ``<Drone>``) into
        a list of ``Waypoint`` objects.
        """
        wp_elems = elem.findall("Waypoint")
        if not wp_elems:
            raise MissionParseError(f"<Waypoints> inside <{drone_ctx}> contains no <Waypoint> elements.")

        waypoints: List[Waypoint] = []
        for wp_elem in wp_elems:
            wp_id_str = wp_elem.get("id") or ""
            if not wp_id_str:
                raise MissionParseError(f"<Waypoint> inside <{drone_ctx}> is missing required attribute 'id'.")

            try:
                wp_id = int(wp_id_str)
            except ValueError as exc:
                raise MissionParseError(
                    f"<Waypoint> attribute 'id' must be an integer, got '{wp_id_str}' in <{drone_ctx}>."
                ) from exc

            ctx = f"{drone_ctx}/Waypoint[{wp_id}]"
            lat = _find_float(wp_elem, "Lat", ctx)
            lon = _find_float(wp_elem, "Lon", ctx)
            alt = _find_float(wp_elem, "Alt", ctx)
            speed = _find_optional_float(wp_elem, "Speed", ctx)

            loiter_text = _find_text(wp_elem, "LoiterSec", "0")
            try:
                loiter_sec = float(loiter_text)
            except ValueError:
                loiter_sec = 0.0

            description = _find_text(wp_elem, "Description", "")

            actions: List[ActionConfig] = []
            actions_mode = "sequential"
            actions_elem = wp_elem.find("Actions")
            if actions_elem is not None:
                actions_mode = (actions_elem.get("mode") or "sequential").strip().lower()
                if actions_mode not in ("sequential", "parallel"):
                    raise MissionParseError(
                        f"<Actions mode=\"{actions_mode}\"> inside {ctx} must be "
                        f"'sequential' or 'parallel'."
                    )
                actions = [
                    MissionParser._parse_action(action_elem, ctx)
                    for action_elem in actions_elem.findall("Action")
                ]

            wp = Waypoint(
                id=wp_id,
                lat=lat,
                lon=lon,
                alt=alt,
                actions=actions,
                actions_mode=actions_mode,
                speed=speed,
                loiter_sec=loiter_sec,
                description=description,
            )
            log.debug(
                "Parsed %s: lat=%.6f lon=%.6f actions=%s mode=%s",
                ctx, lat, lon, [a.code for a in actions] or "none", actions_mode,
            )
            waypoints.append(wp)

        return waypoints

    @staticmethod
    def _parse_action(elem: ET.Element, ctx: str) -> ActionConfig:
        """
        Parse a generic ``<Action code="...">`` element. There is no fixed
        set of valid codes — any string is resolved to a plugin at runtime
        by convention (``plugins/<code>.py``). Parameters are not read
        here; they live in that plugin's own ``plugins/<code>.meta.json``
        sidecar file (see ``core/action_runner.py``).
        """
        code = (elem.get("code") or "").strip()
        if not code:
            raise MissionParseError(f"<Action> inside {ctx} is missing required attribute 'code'.")

        return ActionConfig(code=code)

    @staticmethod
    def _parse_safety(elem: ET.Element) -> SafetyConfig:
        ctx = "Safety"
        return SafetyConfig(
            min_battery_percent=   _find_int(elem,   "MinBatteryPercent",   ctx),
            max_hdop=              _find_float(elem,  "MaxHdop",             ctx),
            heartbeat_timeout_sec= _find_int(elem,   "HeartbeatTimeoutSec", ctx),
            geofence_radius_m=     _find_float(elem,  "GeofenceRadiusM",    ctx),
        )
