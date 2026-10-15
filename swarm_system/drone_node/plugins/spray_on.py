"""
plugins/spray_on.py
--------------------
Action plugin for <Action code="spray_on">. Spraying is not special-cased
anywhere in the system — this is a plugin like any other, resolved purely
by the action code matching this filename.

Requires this Jetson's local drone_config.yaml to define a "spray"
hardware entry, which main.py turns into a SprayController and injects
into drone_context under the "spray" key. If no spray hardware is
configured on this Jetson, this action logs a warning and no-ops rather
than crashing the mission.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger(__name__)


def run(drone_context: Dict[str, Any]) -> None:
    spray = drone_context.get("spray")
    if spray is None:
        log.warning(
            "[%s] Action 'spray_on' requested but no spray hardware is "
            "configured in this Jetson's local config.",
            drone_context.get("drone_id", "?"),
        )
        return
    spray.spray_on()
