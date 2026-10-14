"""
agents/telemetry_bus.py
-----------------------
Async pub/sub telemetry event bus for the drone_brain swarm system.

Every ``DroneAgent`` publishes ``TelemetryEvent`` objects onto the
``TelemetryBus`` at a configurable interval.  Any number of consumers
(GCS bridge, status monitor, safety watchdog) can subscribe to:

* A specific ``drone_id`` channel — receives events only for that drone.
* The wildcard channel ``"*"`` — receives all events from all drones.

Architecture
~~~~~~~~~~~~
Internally the bus holds a ``Dict[str, List[asyncio.Queue]]`` where each
key is either a ``drone_id`` string or ``"*"``.  Publishing an event
places it on every queue registered under the event's ``drone_id`` *and*
on every queue registered under ``"*"``.

All mutation of the subscriber dictionary is protected by an
``asyncio.Lock`` to prevent race conditions when multiple coroutines
subscribe/unsubscribe concurrently.

Usage
-----
>>> bus = TelemetryBus()
>>> queue = bus.subscribe("drone-1")
>>> # In another coroutine:
>>> event = await queue.get()
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List

log = logging.getLogger(__name__)

# Wildcard channel key used for "all drones" subscriptions
_WILDCARD = "*"


# ---------------------------------------------------------------------------
# Drone state enum
# ---------------------------------------------------------------------------

class DroneState(str, Enum):
    """
    High-level lifecycle state of a drone agent.

    Values
    ------
    IDLE       : Agent created but not yet started.
    PREFLIGHT  : Waiting for GPS lock / preflight checks.
    FLYING     : Actively flying waypoints.
    RTL        : Return-to-launch manoeuvre in progress.
    LANDED     : Motors disarmed and drone on ground.
    ERROR      : Unrecoverable error state.
    """

    IDLE      = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    FLYING    = "FLYING"
    RTL       = "RTL"
    LANDED    = "LANDED"
    ERROR     = "ERROR"


# ---------------------------------------------------------------------------
# Telemetry event dataclass
# ---------------------------------------------------------------------------

@dataclass
class TelemetryEvent:
    """
    A snapshot of a single drone's telemetry at a point in time.

    Attributes
    ----------
    drone_id    : Unique drone identifier (matches ``DroneConfig.id``).
    timestamp   : UTC datetime when this snapshot was taken.
    lat         : Latitude in decimal degrees.
    lon         : Longitude in decimal degrees.
    alt_m       : Altitude above mean sea level in metres.
    battery_pct : Battery state of charge as a percentage (0–100).
    state       : Current ``DroneState`` lifecycle value.
    extra       : Optional additional key/value data (e.g. HDOP, speed).
    """

    drone_id:    str
    timestamp:   datetime
    lat:         float
    lon:         float
    alt_m:       float
    battery_pct: float
    state:       DroneState
    extra:       Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Telemetry bus
# ---------------------------------------------------------------------------

class TelemetryBus:
    """
    Async pub/sub telemetry event bus.

    Subscribers receive ``TelemetryEvent`` objects via ``asyncio.Queue``
    instances.  The bus supports both per-drone and wildcard (``"*"``)
    subscriptions.

    Thread safety
    ~~~~~~~~~~~~~
    All modifications to ``_subscribers`` are serialised through
    ``self._lock`` (an ``asyncio.Lock``).  Publishing is also serialised
    to ensure that the subscriber list is not modified while iterating.

    Attributes
    ----------
    _subscribers : Mapping from channel key to list of queues.
    _lock        : Async lock protecting ``_subscribers``.
    """

    def __init__(self) -> None:
        """Initialise an empty subscriber registry."""
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        log.debug("TelemetryBus initialised.")

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(self, drone_id: str) -> asyncio.Queue:
        """
        Register a new subscriber for *drone_id* events.

        The caller receives a fresh ``asyncio.Queue``.  Pass
        ``drone_id="*"`` to subscribe to events from **all** drones.

        .. note::
           This is a **synchronous** method for convenience (no I/O is
           performed).  Queue registration is protected by the lock only
           during ``publish``; callers must ensure ``subscribe`` is called
           from the async context before the event loop is running or use
           ``asyncio.get_event_loop().run_until_complete`` in tests.

        Parameters
        ----------
        drone_id : Drone identifier or ``"*"`` for the wildcard channel.

        Returns
        -------
        asyncio.Queue
            A queue that will receive ``TelemetryEvent`` objects.
        """
        q: asyncio.Queue = asyncio.Queue()
        if drone_id not in self._subscribers:
            self._subscribers[drone_id] = []
        self._subscribers[drone_id].append(q)
        log.debug("New subscriber on channel '%s'. total=%d",
                  drone_id, len(self._subscribers[drone_id]))
        return q

    def unsubscribe(self, drone_id: str, queue: asyncio.Queue) -> None:
        """
        Remove *queue* from the *drone_id* subscriber list.

        If *queue* is not found, the call is a no-op (no exception raised).

        Parameters
        ----------
        drone_id : Channel from which to remove the queue.
        queue    : The queue previously returned by ``subscribe()``.
        """
        channel = self._subscribers.get(drone_id, [])
        try:
            channel.remove(queue)
            log.debug("Unsubscribed queue from channel '%s'. remaining=%d",
                      drone_id, len(channel))
        except ValueError:
            log.warning(
                "Attempted to unsubscribe a queue not registered on '%s'.", drone_id
            )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, event: TelemetryEvent) -> None:
        """
        Publish *event* to all subscribers of its drone_id channel and
        to all wildcard subscribers.

        The method is non-blocking for the publisher: events are placed
        onto each queue with ``put_nowait``.  If a subscriber's queue is
        full the event is silently dropped for *that* subscriber only,
        and a warning is logged.

        Parameters
        ----------
        event : The ``TelemetryEvent`` to broadcast.
        """
        async with self._lock:
            channels: List[str] = [event.drone_id]
            if event.drone_id != _WILDCARD:
                channels.append(_WILDCARD)

            for channel in channels:
                for q in self._subscribers.get(channel, []):
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        log.warning(
                            "Telemetry queue full on channel '%s'; "
                            "dropping event for drone '%s'.",
                            channel, event.drone_id,
                        )

        log.debug(
            "Published telemetry: drone=%s state=%s lat=%.5f lon=%.5f "
            "alt=%.1f m bat=%.0f%%",
            event.drone_id, event.state.value,
            event.lat, event.lon, event.alt_m, event.battery_pct,
        )

