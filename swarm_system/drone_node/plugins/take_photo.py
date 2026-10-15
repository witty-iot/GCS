"""
plugins/take_photo.py
-----------------------
Action plugin for <Action code="take_photo">. This plugin exists mainly
to demonstrate that the mission schema is genuinely generic — a payload
other than spraying needs zero schema changes and zero core code
changes, just a new plugin file named after its action code.

No real camera is wired up yet, so this just logs what it would do.
Replace the body with a real camera trigger (e.g. a gphoto2 call, a
GPIO shutter trigger, or a ROS service call) when camera hardware exists.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger(__name__)


def run(drone_context: Dict[str, Any]) -> None:
    drone_id = drone_context.get("drone_id", "?")
    pos = drone_context.get("position", {})
    log.info(
        "[%s] take_photo: would capture image at (%.6f, %.6f, %.1fm) [SIMULATED — no camera wired up].",
        drone_id, pos.get("lat", 0.0), pos.get("lon", 0.0), pos.get("alt", 0.0),
    )
