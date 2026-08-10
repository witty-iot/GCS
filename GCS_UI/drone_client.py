"""
UDP protocol client for the ESP32 mission bridge -- talks the exact same JSON
mission API (UDP 14551) that Missions/mission_hover_guided_test.py and
mission_hover_loiter_test.py use. No firmware changes needed; this is just
another client of the existing protocol, documented in the top-level README
under "Mission Command Protocol".

Kept dependency-free (stdlib only) and Tk-free so it can be reused or tested
on its own.
"""

import json
import socket


class DroneClient:
    def __init__(self, ip="192.168.4.1", mission_port=14551):
        self.ip = ip
        self.mission_port = mission_port

    # ─── Low-level ───────────────────────────────────────────────────────
    def _send(self, message, expect_reply=False, timeout=2.0, retries=1):
        """UDP has no delivery guarantee. retries resends the same request a
        few times before giving up -- safe here because every command this is
        used for (STATUS, START, STOP, mission upload) is a no-op or
        idempotent to repeat from the ESP32's side."""
        last_exc = None
        for attempt in range(retries):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            try:
                sock.sendto(message.encode("utf-8"), (self.ip, self.mission_port))
                if expect_reply:
                    data, _ = sock.recvfrom(2048)
                    return data.decode("utf-8", errors="replace").strip()
                return None
            except Exception as exc:
                last_exc = exc
                continue
            finally:
                sock.close()
        if not expect_reply and last_exc:
            raise last_exc
        return None

    # ─── Mission API ─────────────────────────────────────────────────────
    def upload_mission(self, steps):
        """steps: list of {"action": ..., <params>}. Returns (ok, reply_text)."""
        data = json.dumps({"steps": steps}, separators=(",", ":"))
        reply = self._send(data, expect_reply=True, timeout=3, retries=2)
        return reply == "OK MISSION_STORED", reply

    def start(self):
        """Returns (ok, detail). A dropped ack does not mean the vehicle didn't
        start -- the ESP32 sends "OK START" before the mission begins
        executing, so this retries and falls back to a STATUS check before
        ever reporting failure, matching Missions/mission_hover_guided_test.py."""
        reply = self._send("START", expect_reply=True, timeout=2, retries=3)
        if reply == "OK START":
            return True, "OK START"
        if reply == "ERROR already_running":
            return True, "already running (earlier START ack was lost)"
        status = self.request_status(timeout=2)
        if status and status.get("running"):
            return True, "running (ack lost, confirmed via STATUS)"
        return False, reply or "no UDP reply"

    def stop(self):
        """Idempotent on the ESP32 (aborts + commands LAND every time), so a
        retry here is always safe. Returns (ok, detail)."""
        reply = self._send("STOP", expect_reply=True, timeout=2, retries=3)
        return (reply is not None), (reply or "no UDP reply")

    def request_status(self, timeout=1.5):
        reply = self._send("STATUS", expect_reply=True, timeout=timeout, retries=1)
        if not reply:
            return None
        try:
            status = json.loads(reply)
        except json.JSONDecodeError:
            return None
        return status if status.get("type") == "status" else None
