"""
plugins/window_clean.py
------------------------
Action plugin for <Action code="window_clean">. Loaded dynamically by
core/action_runner.py by filename convention, same as every other plugin
here — nothing window-cleaning-specific is hardcoded anywhere outside
this file and its sidecar plugins/window_clean.meta.json.

This plugin does NOT own the camera. The camera feed is a persistent
local hardware controller (flight/camera_feed.py, CameraFeed), started
once when the drone begins flying and running the whole mission — live
preview from shortly after takeoff until landing, same lifecycle as the
flight itself. This plugin just tells that already-running feed "start
drawing boxes and counting detections" for the duration of this action,
then reads back what it saw.

Requires this Jetson's local drone_config.yaml to define a "camera"
hardware entry (see flight/camera_feed.py); if none is configured, or
the camera failed to open, this action logs a warning and no-ops rather
than crashing the mission.

Parameters (from plugins/window_clean.meta.json, NOT the mission XML)
-----------------------------------------------------------------------
model_path            : Path to the trained YOLO weights (best.pt).
confidence_threshold    : Minimum detection confidence to count as real.
trigger_classes        : Detected classes that mean "needs cleaning".
pressure_levels         : Detection-count -> spray pressure tiers.
hold_duration_sec       : How long to stay in detection mode at this waypoint.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def _pick_pressure(detection_count: int, pressure_levels: Dict[str, Dict[str, Any]]) -> int:
    """
    Map the worst (highest) qualifying-detection count seen during the
    hold window to a pressure value, using the tiers ordered by
    ascending "max_detections" from plugins/window_clean.meta.json.
    """
    tiers = sorted(pressure_levels.items(), key=lambda kv: kv[1]["max_detections"])
    for _name, tier in tiers:
        if detection_count <= tier["max_detections"]:
            return int(tier["pressure"])
    return int(tiers[-1][1]["pressure"])


async def run(
    drone_context: Dict[str, Any],
    model_path: str,
    confidence_threshold: float = 0.4,
    trigger_classes: List[str] = None,
    pressure_levels: Dict[str, Dict[str, Any]] = None,
    hold_duration_sec: float = 20.0,
) -> None:
    drone_id = drone_context.get("drone_id", "?")
    trigger_classes = trigger_classes or ["dirt", "stain"]
    pressure_levels = pressure_levels or {"default": {"max_detections": 999, "pressure": 100}}

    camera = drone_context.get("camera")
    if camera is None:
        log.warning(
            "[%s] window_clean: no camera configured on this Jetson, skipping.", drone_id,
        )
        return

    log.info(
        "[%s] window_clean: starting %.0fs inspection (threshold=%.2f).",
        drone_id, hold_duration_sec, confidence_threshold,
    )

    camera.enable_detection(model_path, trigger_classes, confidence_threshold)
    await asyncio.sleep(hold_duration_sec)
    max_detection_count = camera.disable_detection()

    if max_detection_count == 0:
        log.info("[%s] window_clean: window is clean, no spray triggered.", drone_id)
        return

    pressure = _pick_pressure(max_detection_count, pressure_levels)
    spray = drone_context.get("spray")
    if spray is None:
        log.warning(
            "[%s] window_clean: needs cleaning (detections=%d, pressure=%d) but "
            "no spray hardware is configured on this Jetson.",
            drone_id, max_detection_count, pressure,
        )
        return

    log.info(
        "[%s] window_clean: needs cleaning (max qualifying detections=%d) -> "
        "spraying at pressure=%d.",
        drone_id, max_detection_count, pressure,
    )
    spray.spray_on(pressure=pressure)
