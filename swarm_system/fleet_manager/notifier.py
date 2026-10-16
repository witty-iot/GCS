"""
notifier.py
-----------
Sends an alert when a drone goes down/disconnected (or comes back).
Posts to a Microsoft Teams incoming webhook if one is configured;
always logs loudly to the console regardless, so an alert is never
silently dropped just because no webhook is set up yet.

Setup
-----
In Teams: the channel you want alerts in -> "..." -> Workflows ->
"Post to a channel when a webhook request is received" -> copy the
URL it gives you. Pass it to fleet_manager.py via --webhook-url, or
set it once as an env var:

    export SWARM_NOTIFY_WEBHOOK="https://..."
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

log = logging.getLogger("fleet_manager.notifier")


class Notifier:
    def __init__(self, webhook_url: Optional[str] = None) -> None:
        self.webhook_url = webhook_url or None
        if self.webhook_url:
            log.info("Notifier: alerts will be posted to configured webhook.")
        else:
            log.info("Notifier: no webhook configured — alerts will only be logged here.")

    def _send(self, text: str) -> None:
        log.warning("ALERT: %s", text)
        if not self.webhook_url:
            return
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            log.error("Notifier: failed to post alert to webhook (%s). Alert text was: %s", exc, text)

    def drone_down(self, drone_id: str, silent_for_sec: float) -> None:
        self._send(
            f"[DRONE DOWN] '{drone_id}' has not sent telemetry for "
            f"{silent_for_sec:.0f}s — check its link/power. Treating it as "
            f"disconnected until it reports back in."
        )

    def drone_recovered(self, drone_id: str) -> None:
        self._send(f"[DRONE RECOVERED] '{drone_id}' is back online — telemetry resumed.")
