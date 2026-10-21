"""
flight/safety.py
-----------------
Real implementations of the spray hardware controller and the in-flight
safety watchdog.

An earlier prototype referenced ``spray_controller`` and ``safety_watchdog``
modules that were never actually included — those imports silently failed
and both features were no-ops. This module provides working replacements:

* ``SprayController`` drives a real GPIO pin on the Jetson via Jetson.GPIO,
  and falls back to a logged simulation when GPIO isn't available (laptop
  dev work, SITL testing) so the same code path runs everywhere.
* ``SafetyWatchdog`` evaluates live telemetry against ``SafetyConfig``
  thresholds (battery, geofence) and exposes a simple ``triggered`` flag
  the flight loop checks between waypoints — no missing dependency, no
  silent no-op.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from core.mission_plan import SafetyConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spray hardware controller
# ---------------------------------------------------------------------------

class SprayController:
    """
    GPIO-backed pump/relay controller.

    Uses ``Jetson.GPIO`` when available. If the library can't be imported
    (e.g. running on a laptop for development, or in SITL), it logs what
    *would* happen instead of raising — the mission logic above it never
    needs to know the difference.
    """

    def __init__(self, gpio_pin: int, pump_on_delay_ms: int, pump_off_delay_ms: int) -> None:
        self.gpio_pin = gpio_pin
        self.pump_on_delay_ms = pump_on_delay_ms
        self.pump_off_delay_ms = pump_off_delay_ms
        self._gpio = None

        try:
            import Jetson.GPIO as GPIO  # type: ignore[import-not-found]
            GPIO.setmode(GPIO.BOARD)
            GPIO.setup(self.gpio_pin, GPIO.OUT, initial=GPIO.LOW)
            self._gpio = GPIO
            log.info("SprayController: Jetson.GPIO active on pin %d.", gpio_pin)
        except Exception as exc:
            log.warning(
                "SprayController: Jetson.GPIO unavailable (%s). "
                "Running in SIMULATION mode — actions will be logged only.",
                exc,
            )

    def spray_on(self, pressure: int = 100) -> None:
        """
        Turn the pump on. ``pressure`` (0-100) is accepted for callers
        that derive a spray intensity from something upstream (e.g. the
        window_clean vision action) — logged either way, but only
        actually varies the hardware output once the pump is wired to a
        PWM-capable channel instead of today's plain digital on/off pin.
        """
        if self._gpio is not None:
            self._gpio.output(self.gpio_pin, self._gpio.HIGH)
            log.info("Pump ON (GPIO %d) pressure=%d%%.", self.gpio_pin, pressure)
        else:
            log.info("Pump ON (GPIO %d) pressure=%d%% [SIMULATED].", self.gpio_pin, pressure)

    def emergency_stop(self) -> None:
        """
        Generic shutdown hook. DroneAgent calls this on any hardware
        controller it was handed (duck-typed, no spray-specific knowledge
        in the flight code) when a mission aborts or errors — it doesn't
        know or care that this happens to be a spray controller.
        """
        self.spray_off()

    def spray_off(self) -> None:
        if self._gpio is not None:
            self._gpio.output(self.gpio_pin, self._gpio.LOW)
            log.info("Pump OFF (GPIO %d).", self.gpio_pin)
        else:
            log.info("Pump OFF (GPIO %d) [SIMULATED].", self.gpio_pin)

    def cleanup(self) -> None:
        if self._gpio is not None:
            self._gpio.cleanup(self.gpio_pin)


# ---------------------------------------------------------------------------
# Safety watchdog
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class SafetyWatchdog:
    """
    Evaluates live telemetry against ``SafetyConfig`` thresholds.

    This is deliberately simple and synchronous: the flight loop calls
    ``check()`` on every telemetry update (battery + position already
    being polled anyway) and reacts to ``triggered`` becoming True.
    No background task, no extra MAVSDK connection, no missing module.
    """

    def __init__(self, safety: SafetyConfig, home_lat: float, home_lon: float) -> None:
        self.safety = safety
        self.home_lat = home_lat
        self.home_lon = home_lon
        self.triggered = False
        self.reason: Optional[str] = None

    def check(self, lat: float, lon: float, battery_pct: float) -> bool:
        """
        Evaluate current telemetry. Returns True (and sets ``reason``) the
        first time a threshold is breached; stays True afterwards until
        ``reset()`` is called.
        """
        if self.triggered:
            return True

        if battery_pct < self.safety.min_battery_percent:
            self.triggered = True
            self.reason = (
                f"battery {battery_pct:.0f}% below minimum "
                f"{self.safety.min_battery_percent}%"
            )
            log.warning("SafetyWatchdog triggered: %s", self.reason)
            return True

        dist = haversine_m(lat, lon, self.home_lat, self.home_lon)
        if dist > self.safety.geofence_radius_m:
            self.triggered = True
            self.reason = (
                f"distance from home {dist:.0f}m exceeds geofence "
                f"{self.safety.geofence_radius_m:.0f}m"
            )
            log.warning("SafetyWatchdog triggered: %s", self.reason)
            return True

        return False

    def reset(self) -> None:
        self.triggered = False
        self.reason = None
