"""
swarm/mqtt_link.py
--------------------
Bridges this drone's process to the rest of the swarm over MQTT. Four jobs:

1. Registration — ask the laptop-side fleet_manager "who am I?" using this
   Jetson's stable hardware_id, and receive back a drone_id + mission XML.
2. Telemetry broadcast — publish this drone's position/battery/state so
   every other drone (and the laptop dashboard) can see it.
3. Telemetry ingestion — subscribe to every other drone's telemetry topic
   and feed it into the local SwarmState.
4. Ground-triggered launch — fleet_manager is the scheduling authority: it
   watches the mission's <StartDate> (or an operator's explicit "go")
   and broadcasts one "swarm/launch" message to every drone at once, for
   real synchronized swarm takeoff. Each drone's own local scheduler
   (core/scheduler.py, watching the same <StartDate>) stays armed the
   whole time as a fallback — if the broker/ground link is unreachable
   right at launch time, the drone still launches itself locally rather
   than waiting forever for a signal that may never arrive.

If the broker is unreachable (no laptop/network, or testing a single drone
with nothing else running), this class degrades gracefully: connect() marks
itself as disconnected, register() returns None immediately, and the drone
runs standalone off a locally cached mission file. Nothing here is a hard
dependency for flight.

paho-mqtt's client runs its own network thread with synchronous callbacks;
this class bridges those callbacks into the asyncio event loop via
``call_soon_threadsafe`` so the rest of the system stays async-native.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import paho.mqtt.client as mqtt

from swarm.swarm_state import SwarmState

log = logging.getLogger(__name__)

TELEMETRY_TOPIC_PREFIX = "swarm/telemetry"
REGISTER_REQUEST_TOPIC = "swarm/register/request"
REGISTER_ACK_PREFIX = "swarm/register/ack"
LAUNCH_TOPIC = "swarm/launch"


class SwarmLink:
    def __init__(
        self,
        broker_host: str,
        broker_port: int,
        hardware_id: str,
        swarm_state: SwarmState,
        connect_timeout_sec: float = 5.0,
    ) -> None:
        self.hardware_id = hardware_id
        self.drone_id: Optional[str] = None
        self.swarm_state = swarm_state
        self.connected = False

        self._loop = asyncio.get_event_loop()
        self._register_ack_queue: asyncio.Queue = asyncio.Queue()
        self._launch_event: asyncio.Event = asyncio.Event()

        self._client = mqtt.Client(client_id=f"drone-{hardware_id}", clean_session=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        try:
            self._client.connect(broker_host, broker_port, keepalive=15)
            self._client.loop_start()
            self.connected = True
            log.info("SwarmLink: connecting to broker %s:%d ...", broker_host, broker_port)
        except Exception as exc:
            log.warning(
                "SwarmLink: broker %s:%d unreachable (%s). "
                "Running STANDALONE — no swarm awareness, no central mission push.",
                broker_host, broker_port, exc,
            )
            self.connected = False

    # ------------------------------------------------------------------
    # paho callbacks (run on the MQTT network thread)
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            log.warning("SwarmLink: broker connect failed, rc=%s", rc)
            return
        log.info("SwarmLink: connected to broker.")
        client.subscribe(f"{REGISTER_ACK_PREFIX}/{self.hardware_id}")
        client.subscribe(f"{TELEMETRY_TOPIC_PREFIX}/+")
        client.subscribe(LAUNCH_TOPIC)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            log.warning("SwarmLink: dropped malformed message on %s: %s", msg.topic, exc)
            return

        if msg.topic == f"{REGISTER_ACK_PREFIX}/{self.hardware_id}":
            self._loop.call_soon_threadsafe(self._register_ack_queue.put_nowait, payload)
            return

        if msg.topic == LAUNCH_TOPIC:
            log.info("SwarmLink: ground launch signal received.")
            self._loop.call_soon_threadsafe(self._launch_event.set)
            return

        if msg.topic.startswith(f"{TELEMETRY_TOPIC_PREFIX}/"):
            peer_drone_id = msg.topic.rsplit("/", 1)[-1]
            if peer_drone_id == self.drone_id:
                return  # ignore our own echoed telemetry
            self._loop.call_soon_threadsafe(self.swarm_state.update, peer_drone_id, payload)

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def register(self, timeout_sec: float = 10.0) -> Optional[dict]:
        """
        Ask fleet_manager for an ID + mission. Returns the ack payload
        (``{"drone_id": ..., "mission_xml": ...}``) or None if the broker
        is unreachable or no response arrives in time — in which case the
        caller should fall back to a locally cached/provided mission file.
        """
        if not self.connected:
            return None

        self._client.publish(
            REGISTER_REQUEST_TOPIC,
            json.dumps({"hardware_id": self.hardware_id}),
            qos=1,
        )
        log.info("SwarmLink: registration request sent (hardware_id=%s).", self.hardware_id)

        try:
            ack = await asyncio.wait_for(self._register_ack_queue.get(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            log.warning(
                "SwarmLink: no registration ack within %.0fs; falling back to local mission.",
                timeout_sec,
            )
            return None

        self.drone_id = ack.get("drone_id")
        log.info("SwarmLink: registered as drone_id=%s.", self.drone_id)
        return ack

    async def wait_for_launch(self) -> None:
        """
        Block until fleet_manager broadcasts the ground launch signal.
        If the broker is unreachable, this simply never completes — the
        caller is expected to race it against the local scheduler fallback
        (see main.py), not rely on it alone.
        """
        await self._launch_event.wait()

    def publish_telemetry(self, payload: dict) -> None:
        if not self.connected or self.drone_id is None:
            return
        self._client.publish(
            f"{TELEMETRY_TOPIC_PREFIX}/{self.drone_id}",
            json.dumps(payload),
            qos=0,
        )

    def close(self) -> None:
        if self.connected:
            self._client.loop_stop()
            self._client.disconnect()
