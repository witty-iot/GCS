"""
plugins/spray_pattern.py
------------------------
Action plugin for <Action code="spray_pattern">. Loaded dynamically by
``core/action_runner.py`` purely by filename convention — this file is
no different from spray_on.py or take_photo.py as far as the system is
concerned, it just happens to implement spraying.

The entry point must be named ``run`` and receives the ``drone_context``
dict containing:
  - ``drone_id``     : str — unique identifier of the executing drone.
  - ``position``     : dict with keys ``lat``, ``lon``, ``alt`` (floats).
  - ``battery_pct``  : float — current battery percentage (0–100).
  - ``mavsdk_system``: mavsdk.System handle for advanced MAVSDK commands.
  - ``spray``        : SprayController, if this Jetson's local config
                       defines spray hardware (see spray_on.py/spray_off.py).

``pattern``, ``passes``, ``pass_duration_sec`` and ``inter_pass_pause_sec``
are no longer authored in the mission XML — they come from this plugin's
sidecar ``plugins/spray_pattern.meta.json`` file, loaded by
``core/action_runner.py``. Every waypoint that runs ``spray_pattern``
anywhere, in any mission, gets the same params from that one file; there
is no longer a per-waypoint/per-mission override.

Supported patterns
------------------
zigzag
    Drone simulates a back-and-forth spray sweep. Each pass logs the
    heading reversal and simulates the physical movement time with an
    ``asyncio.sleep``.
linear
    Drone simulates a straight single-direction spray sweep for each pass.
figure8
    Drone simulates a figure-8 overlap spray pattern (wide-area coverage).
spiral
    Drone simulates an expanding outward spiral from the current position.

Adding new patterns
-------------------
Add a new ``elif pattern == "my_pattern":`` branch inside ``run()`` and
implement it following the existing style. For patterns that require
actual MAVSDK movement commands, retrieve the system handle via
``drone_context["mavsdk_system"]``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

log = logging.getLogger(__name__)


async def run(
    drone_context: Dict[str, Any],
    pattern: str = "zigzag",
    passes: int = 3,
    pass_duration_sec: float = 2.0,
    inter_pass_pause_sec: float = 0.5,
) -> None:
    """
    Execute a configurable spray pattern for one waypoint segment.

    This coroutine is called by ``ActionRunner.run()`` and receives the
    drone's live context plus whatever keyword arguments this plugin's
    own ``plugins/spray_pattern.meta.json`` sidecar file provides.

    Parameters
    ----------
    drone_context        : Runtime context injected by ``ActionRunner``.
                            Keys: ``drone_id``, ``position``,
                            ``battery_pct``, ``mavsdk_system``.
    pattern               : Name of the spray pattern to execute. One of:
                            ``"zigzag"``, ``"linear"``, ``"figure8"``,
                            ``"spiral"``. Defaults to ``"zigzag"``.
    passes                : Number of passes (sweeps) to simulate. Must be >= 1.
    pass_duration_sec     : Simulated time per spray pass (drone travel time).
    inter_pass_pause_sec  : Brief pause between consecutive passes (pump
                            flush / heading change).

    Returns
    -------
    None
        This plugin does not return a meaningful value.

    Notes
    -----
    * In a production deployment replace the ``asyncio.sleep`` calls with
      real MAVSDK ``goto_location`` commands derived from the field boundary
      geometry stored in ``drone_context["position"]``.
    * Battery is checked before each pass; execution halts early with a
      warning if the drone's battery drops below 15 %.
    """
    drone_id:    str   = drone_context.get("drone_id", "unknown")
    position:    dict  = drone_context.get("position", {})
    battery_pct: float = drone_context.get("battery_pct", 100.0)

    lat = position.get("lat", 0.0)
    lon = position.get("lon", 0.0)
    alt = position.get("alt", 0.0)

    log.info(
        "[%s] execute_spray_pattern: pattern='%s' passes=%d "
        "pos=(%.6f, %.6f, %.1fm) battery=%.0f%%",
        drone_id, pattern, passes, lat, lon, alt, battery_pct,
    )

    # Guard: clamp passes to a sane range
    passes = max(1, min(passes, 20))

    for pass_num in range(1, passes + 1):
        # Pre-pass battery check
        if battery_pct < 15.0:
            log.warning(
                "[%s] Battery critically low (%.0f%%). Aborting spray pattern after %d/%d passes.",
                drone_id, battery_pct, pass_num - 1, passes,
            )
            return

        if pattern == "zigzag":
            direction = "→" if pass_num % 2 == 1 else "←"
            log.info(
                "[%s] Zigzag pass %d/%d  direction=%s  spraying …",
                drone_id, pass_num, passes, direction,
            )
            await asyncio.sleep(pass_duration_sec)
            log.info(
                "[%s] Zigzag pass %d/%d complete. Reversing heading.",
                drone_id, pass_num, passes,
            )

        elif pattern == "linear":
            log.info(
                "[%s] Linear pass %d/%d  direction=→  spraying …",
                drone_id, pass_num, passes,
            )
            await asyncio.sleep(pass_duration_sec)
            log.info(
                "[%s] Linear pass %d/%d complete. Repositioning for next strip.",
                drone_id, pass_num, passes,
            )

        elif pattern == "figure8":
            log.info(
                "[%s] Figure-8 loop %d/%d  spraying left lobe …",
                drone_id, pass_num, passes,
            )
            await asyncio.sleep(pass_duration_sec)
            log.info(
                "[%s] Figure-8 loop %d/%d  spraying right lobe …",
                drone_id, pass_num, passes,
            )
            await asyncio.sleep(pass_duration_sec)
            log.info(
                "[%s] Figure-8 loop %d/%d complete.",
                drone_id, pass_num, passes,
            )

        elif pattern == "spiral":
            radius_m = pass_num * 5.0  # expanding radius per pass
            log.info(
                "[%s] Spiral pass %d/%d  radius=%.1f m  spraying …",
                drone_id, pass_num, passes, radius_m,
            )
            await asyncio.sleep(pass_duration_sec * 1.5)
            log.info(
                "[%s] Spiral pass %d/%d complete.",
                drone_id, pass_num, passes,
            )

        else:
            log.warning(
                "[%s] Unknown spray pattern '%s'. Executing fallback linear pass %d/%d.",
                drone_id, pattern, pass_num, passes,
            )
            await asyncio.sleep(pass_duration_sec)

        # Pause between passes (except after the last one)
        if pass_num < passes:
            await asyncio.sleep(inter_pass_pause_sec)

    log.info(
        "[%s] execute_spray_pattern finished: pattern='%s' passes=%d.",
        drone_id, pattern, passes,
    )

