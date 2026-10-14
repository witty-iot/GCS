"""
swarm/swarm_state.py
---------------------
In-memory view of every other drone in the swarm, built entirely from
telemetry received over MQTT. This is what lets a drone "know what the
other drones are doing" without any central brain making decisions for it.

Works identically whether there are 0 peers (single-drone mode) or N —
nothing here assumes more than one drone exists.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PeerState:
    drone_id: str
    lat: float
    lon: float
    alt_m: float
    battery_pct: float
    state: str
    last_seen: float = field(default_factory=time.time)


class SwarmState:
    """
    Thread/coroutine-safe-enough in-memory peer registry.

    Only ever mutated from the asyncio event loop thread (the MQTT client's
    network thread hands updates over via ``loop.call_soon_threadsafe``
    before they reach here), so no explicit lock is needed.
    """

    def __init__(self, stale_after_sec: float = 15.0) -> None:
        self._peers: Dict[str, PeerState] = {}
        self.stale_after_sec = stale_after_sec

    def update(self, drone_id: str, payload: dict) -> None:
        self._peers[drone_id] = PeerState(
            drone_id=drone_id,
            lat=payload.get("lat", 0.0),
            lon=payload.get("lon", 0.0),
            alt_m=payload.get("alt_m", 0.0),
            battery_pct=payload.get("battery_pct", 100.0),
            state=payload.get("state", "UNKNOWN"),
        )

    def peers(self) -> List[PeerState]:
        """Return all peers heard from within ``stale_after_sec`` seconds."""
        now = time.time()
        return [p for p in self._peers.values() if now - p.last_seen <= self.stale_after_sec]

    def get(self, drone_id: str) -> Optional[PeerState]:
        return self._peers.get(drone_id)

    def any_peer_critical_battery(self, threshold_pct: float) -> Optional[PeerState]:
        """Example reactive query: is any peer below a battery threshold?"""
        for p in self.peers():
            if p.battery_pct < threshold_pct:
                return p
        return None
