"""
plugins/spray_off.py
---------------------
Action plugin for <Action code="spray_off">. See spray_on.py for the
full explanation — this is the mirror-image action.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger(__name__)


def run(drone_context: Dict[str, Any]) -> None:
    spray = drone_context.get("spray")
    if spray is None:
        log.warning(
            "[%s] Action 'spray_off' requested but no spray hardware is "
            "configured in this Jetson's local config.",
            drone_context.get("drone_id", "?"),
        )
        return
    spray.spray_off()
