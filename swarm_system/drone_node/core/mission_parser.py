"""
core/mission_parser.py
----------------------
Parses a swarm mission XML file (validated against mission_schema.xsd)
into a ``MissionPlan`` object ready for consumption by the scheduler
and swarm coordinator.

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
from typing import Any, Dict, List, Optional

from core.mission_plan import (
    CustomCodeConfig,
    DroneConfig,
    MissionPlan,
    SafetyConfig,
    ScheduleConfig,
    SprayConfig,
    Waypoint,
    WaypointAction,
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
    """
    Assert that *element* is not None.

    Parameters
    ----------
    element : The result of an ``ET.Element.find()`` call.
    tag     : XML tag name (used in the error message).
    context : Parent path string (used in the error message).

    Returns
    -------
    ET.Element
        *element* unchanged.

    Raises
    ------
    MissionParseError
        When *element* is ``None``.
    """
    if element is None:
        raise MissionParseError(
            f"Required element <{tag}> is missing inside <{context}>."
        )
    return element


def _require_text(element: ET.Element, tag: str, context: str) -> str:
    """
    Return the stripped text content of *element*, raising
    ``MissionParseError`` when the text is absent or blank.
    """
    text = (element.text or "").strip()
    if not text:
        raise MissionParseError(
            f"Required element <{tag}> inside <{context}> is empty."
        )
    return text


def _find_text(element: ET.Element, tag: str, default: str = "") -> str:
    """Return stripped text of a child element, or *default* if absent."""
    child = element.find(tag)
    if child is None or not child.text:
        return default
    return child.text.strip()


def _find_float(element: ET.Element, tag: str, context: str) -> float:
    """Parse a required child element as a float."""
    child = _require(element.find(tag), tag, context)
    text  = _require_text(child, tag, context)
    try:
        return float(text)
    except ValueError as exc:
        raise MissionParseError(
            f"<{tag}> inside <{context}> is not a valid number: '{text}'"
        ) from exc


def _find_int(element: ET.Element, tag: str, context: str) -> int:
    """Parse a required child element as an integer."""
    child = _require(element.find(tag), tag, context)
    text  = _require_text(child, tag, context)
    try:
        return int(text)
    except ValueError as exc:
        raise MissionParseError(
            f"<{tag}> inside <{context}> is not a valid integer: '{text}'"
        ) from exc


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class MissionParser:
    """
    Converts a swarm mission XML file into a ``MissionPlan`` dataclass.

    Only static/class methods are exposed — instantiation is not needed.

    Example
    -------
    >>> plan = MissionParser.parse("missions/swarm_mission_alpha.xml")
    """

    @staticmethod
    def parse(xml_path: str) -> MissionPlan:
        """
        Parse the XML file at *xml_path* and return a fully populated
        ``MissionPlan``.

        Parameters
        ----------
        xml_path : Absolute or relative path to the mission XML file.

        Returns
        -------
        MissionPlan
            A validated, fully populated mission plan.

        Raises
        ------
        MissionParseError
            On any missing required field or type-conversion error.
        FileNotFoundError
            When *xml_path* does not exist on disk.
        ET.ParseError
            When the file is not well-formed XML.
        """
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

        schedule  = MissionParser._parse_schedule(
            _require(root.find("Schedule"), "Schedule", "Mission")
        )
        drones    = MissionParser._parse_fleet(
            _require(root.find("Fleet"), "Fleet", "Mission")
        )
        waypoints = MissionParser._parse_waypoints(
            _require(root.find("Waypoints"), "Waypoints", "Mission")
        )
        safety    = MissionParser._parse_safety(
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
            waypoints=waypoints,
            safety=safety,
            rtl_after_mission=rtl_after,
        )

        log.info(
            "Mission '%s' parsed successfully: %d drone(s), %d waypoint(s).",
            plan.name, len(plan.drones), len(plan.waypoints),
        )
        return plan

    # ------------------------------------------------------------------
    # Section parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_schedule(elem: ET.Element) -> ScheduleConfig:
        """
        Parse the ``<Schedule>`` element into a ``ScheduleConfig``.

        Parameters
        ----------
        elem : The ``<Schedule>`` XML element.

        Returns
        -------
        ScheduleConfig

        Raises
        ------
        MissionParseError
            When ``<StartDate>`` or ``<TimeZone>`` are missing/blank,
            or when ``<StartDate>`` cannot be parsed as ISO 8601.
        """
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
        Parse the ``<Fleet>`` element into a list of ``DroneConfig`` objects.

        Parameters
        ----------
        elem : The ``<Fleet>`` XML element.

        Returns
        -------
        List[DroneConfig]
            At least one drone config.

        Raises
        ------
        MissionParseError
            When no ``<Drone>`` children are found or any required sub-element
            is missing.
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

            conn   = _require_text(
                _require(d.find("ConnectionString"), "ConnectionString", f"Drone[{drone_id}]"),
                "ConnectionString", f"Drone[{drone_id}]",
            )
            t_alt  = _find_float(d, "TakeoffAltitude", f"Drone[{drone_id}]")
            speed  = _find_float(d, "DefaultSpeed",    f"Drone[{drone_id}]")
            spray  = MissionParser._parse_spray(
                _require(d.find("SpraySettings"), "SpraySettings", f"Drone[{drone_id}]"),
                drone_id,
            )

            cfg = DroneConfig(
                id=drone_id,
                name=drone_name,
                connection_string=conn,
                takeoff_altitude_m=t_alt,
                default_speed_ms=speed,
                spray=spray,
            )
            log.debug("Parsed drone: %s (%s) → %s", drone_name, drone_id, conn)
            drones.append(cfg)

        return drones

    @staticmethod
    def _parse_spray(elem: ET.Element, drone_id: str) -> SprayConfig:
        """
        Parse a ``<SpraySettings>`` element into a ``SprayConfig``.

        Parameters
        ----------
        elem     : The ``<SpraySettings>`` XML element.
        drone_id : Parent drone id (used in error messages).

        Returns
        -------
        SprayConfig
        """
        ctx = f"SpraySettings[Drone={drone_id}]"
        return SprayConfig(
            gpio_pin=          _find_int(elem, "GpioPin",         ctx),
            pump_on_delay_ms=  _find_int(elem, "PumpOnDelayMs",   ctx),
            pump_off_delay_ms= _find_int(elem, "PumpOffDelayMs",  ctx),
        )

    @staticmethod
    def _parse_waypoints(elem: ET.Element) -> List[Waypoint]:
        """
        Parse the ``<Waypoints>`` element into a list of ``Waypoint`` objects.

        Parameters
        ----------
        elem : The ``<Waypoints>`` XML element.

        Returns
        -------
        List[Waypoint]
            Ordered list of all waypoints across all drones.

        Raises
        ------
        MissionParseError
            When no ``<Waypoint>`` children are present or any required
            sub-element is missing.
        """
        wp_elems = elem.findall("Waypoint")
        if not wp_elems:
            raise MissionParseError("<Waypoints> contains no <Waypoint> elements.")

        waypoints: List[Waypoint] = []
        for wp_elem in wp_elems:
            wp_id_str = wp_elem.get("id") or ""
            drone_id  = wp_elem.get("drone_id") or ""

            if not wp_id_str:
                raise MissionParseError("<Waypoint> element is missing required attribute 'id'.")
            if not drone_id:
                raise MissionParseError(
                    f"<Waypoint id='{wp_id_str}'> is missing required attribute 'drone_id'."
                )

            try:
                wp_id = int(wp_id_str)
            except ValueError as exc:
                raise MissionParseError(
                    f"<Waypoint> attribute 'id' must be an integer, got '{wp_id_str}'."
                ) from exc

            ctx    = f"Waypoint[{wp_id}]"
            lat    = _find_float(wp_elem, "Lat", ctx)
            lon    = _find_float(wp_elem, "Lon", ctx)
            alt    = _find_float(wp_elem, "Alt", ctx)

            action_text = _require_text(
                _require(wp_elem.find("Action"), "Action", ctx),
                "Action", ctx,
            )
            try:
                action = WaypointAction(action_text)
            except ValueError as exc:
                raise MissionParseError(
                    f"<Action> value '{action_text}' in {ctx} is not one of "
                    f"{[a.value for a in WaypointAction]}."
                ) from exc

            loiter_text  = _find_text(wp_elem, "LoiterSec", "0")
            try:
                loiter_sec = float(loiter_text)
            except ValueError:
                loiter_sec = 0.0

            description  = _find_text(wp_elem, "Description", "")
            custom_code  = None

            if action == WaypointAction.CUSTOM_CODE:
                cc_elem = wp_elem.find("CustomCode")
                if cc_elem is None:
                    raise MissionParseError(
                        f"{ctx} has action='custom_code' but no <CustomCode> child."
                    )
                custom_code = MissionParser._parse_custom_code(cc_elem, ctx)

            wp = Waypoint(
                id=wp_id,
                drone_id=drone_id,
                lat=lat,
                lon=lon,
                alt=alt,
                action=action,
                loiter_sec=loiter_sec,
                description=description,
                custom_code=custom_code,
            )
            log.debug("Parsed %s: drone=%s lat=%.6f lon=%.6f action=%s",
                      ctx, drone_id, lat, lon, action.value)
            waypoints.append(wp)

        return waypoints

    @staticmethod
    def _parse_custom_code(elem: ET.Element, ctx: str) -> CustomCodeConfig:
        """
        Parse a ``<CustomCode>`` element into a ``CustomCodeConfig``.

        Parameters
        ----------
        elem : The ``<CustomCode>`` XML element.
        ctx  : Context string for error messages (e.g. ``"Waypoint[2]"``).

        Returns
        -------
        CustomCodeConfig
        """
        module_path = _require_text(
            _require(elem.find("Module"), "Module", f"{ctx}/CustomCode"),
            "Module", f"{ctx}/CustomCode",
        )
        function_name = _require_text(
            _require(elem.find("Function"), "Function", f"{ctx}/CustomCode"),
            "Function", f"{ctx}/CustomCode",
        )

        args: Dict[str, Any] = {}
        args_elem = elem.find("Args")
        if args_elem is not None:
            for arg in args_elem.findall("Arg"):
                key   = arg.get("key") or ""
                value = (arg.text or "").strip()
                if not key:
                    raise MissionParseError(
                        f"<Arg> inside {ctx}/CustomCode/Args is missing 'key' attribute."
                    )
                # Attempt int / float coercion; fall back to str
                for converter in (int, float):
                    try:
                        value = converter(value)  # type: ignore[assignment]
                        break
                    except ValueError:
                        pass
                args[key] = value

        return CustomCodeConfig(
            module_path=module_path,
            function_name=function_name,
            args=args,
        )

    @staticmethod
    def _parse_safety(elem: ET.Element) -> SafetyConfig:
        """
        Parse the ``<Safety>`` element into a ``SafetyConfig``.

        Parameters
        ----------
        elem : The ``<Safety>`` XML element.

        Returns
        -------
        SafetyConfig
        """
        ctx = "Safety"
        return SafetyConfig(
            min_battery_percent=   _find_int(elem,   "MinBatteryPercent",   ctx),
            max_hdop=              _find_float(elem,  "MaxHdop",             ctx),
            heartbeat_timeout_sec= _find_int(elem,   "HeartbeatTimeoutSec", ctx),
            geofence_radius_m=     _find_float(elem,  "GeofenceRadiusM",    ctx),
        )

