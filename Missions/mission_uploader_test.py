"""
ESP32 Test Script — Arm, Relay ON 5s, Relay OFF, Disarm
"""

import socket
import json
import time
import sys

ESP32_IP   = "192.168.4.1"
ESP32_PORT = 14551
TIMEOUT = 3
STATUS_INTERVAL = 1.0
PRESTART_TIMEOUT = 20
MISSION_TIMEOUT = 75
FRESH_TELEMETRY_MS = 5000

MISSION = {
    "steps": [
        { "action": "arm" },
        { "action": "relay_on" },
        { "action": "wait",      "seconds": 5 },
        { "action": "relay_off" },
        { "action": "disarm" }
    ]
}

def send(msg, expect_reply=False, timeout=TIMEOUT, quiet=False):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg.encode("utf-8"), (ESP32_IP, ESP32_PORT))
        if not quiet:
            print(f"  -> sent ({len(msg)} bytes)")
        if expect_reply:
            data, _ = sock.recvfrom(2048)
            return data.decode("utf-8", errors="replace").strip()
    except Exception as e:
        if expect_reply:
            return None
        print(f"  X Failed: {e}")
        sys.exit(1)
    finally:
        sock.close()
    return None

def request_status(timeout=TIMEOUT):
    reply = send("STATUS", expect_reply=True, timeout=timeout, quiet=True)
    if not reply:
        return None
    try:
        status = json.loads(reply)
    except json.JSONDecodeError:
        return None
    return status if status.get("type") == "status" else None

def step_label(status):
    step = status.get("step", 0)
    total = status.get("total_steps", 0)
    action = status.get("action") or "idle"
    if total:
        return f"{step + 1}/{total} {action}" if step < total else f"{total}/{total} complete"
    return "no mission"

def print_status(status, prefix="STATUS"):
    gps_age = status.get("gps_age_ms")
    hb_age = status.get("heartbeat_age_ms")
    gps_age_text = "n/a" if gps_age is None or gps_age > 60000 else f"{gps_age / 1000:.1f}s"
    hb_age_text = "n/a" if hb_age is None or hb_age > 60000 else f"{hb_age / 1000:.1f}s"
    gps_fix = status.get("gps_fix_type", 0)
    sats = status.get("gps_satellites", 0)
    ekf = status.get("ekf_flags", 0)
    landed = status.get("landed_state", 0)
    ack_cmd = status.get("last_ack_command", 0)
    ack_result = status.get("last_ack_result", 255)
    print(
        f"[{prefix}] step={step_label(status)}  running={'YES' if status.get('running') else 'NO'}  "
        f"armed={'YES' if status.get('armed') else 'NO'}  gps={'OK' if status.get('gps') else 'NO'} "
        f"fix={gps_fix} sats={sats} age={gps_age_text}  hb_age={hb_age_text} "
        f"ekf=0x{ekf:04X} landed={landed} ack={ack_cmd}/{ack_result} "
        f"alt={status.get('alt', 0):.1f}m  "
        f"completed={'YES' if status.get('completed') else 'NO'} aborted={'YES' if status.get('aborted') else 'NO'}"
    )
    if status.get("last_abort"):
        print(f"         last_abort={status['last_abort']}")

def telemetry_is_fresh(status):
    return status.get("heartbeat_age_ms", 999999) <= FRESH_TELEMETRY_MS

def require_prestart_ready():
    print("[Preflight] Waiting for fresh ESP/Pixhawk heartbeat telemetry...")
    deadline = time.time() + PRESTART_TIMEOUT
    last_status = None
    while time.time() < deadline:
        status = request_status(timeout=1.5)
        if status:
            last_status = status
            print_status(status, "Preflight")
            if not status.get("mission_stored"):
                print("[ERROR] ESP32 does not report a stored mission.")
                sys.exit(1)
            if status.get("running"):
                print("[ERROR] ESP32 says a mission is already running.")
                sys.exit(1)
            if telemetry_is_fresh(status):
                print("[Preflight] Fresh Pixhawk heartbeat telemetry is live.")
                print("[Preflight] Pixhawk arming checks remain enabled and will decide whether ARM is accepted.")
                return
        else:
            print("[Preflight] No UDP status reply from ESP32 yet...")
        time.sleep(STATUS_INTERVAL)

    print("\n[ERROR] Preflight did not become ready.")
    if last_status:
        print_status(last_status, "Last")
    else:
        print("        No STATUS reply received. Flash the updated ESP firmware, check WiFi/IP/port.")
    sys.exit(1)

def monitor_mission():
    print("[3] Monitoring mission telemetry. Press Ctrl+C to send STOP/LAND.")
    deadline = time.time() + MISSION_TIMEOUT
    arm_confirmed = False
    try:
        while time.time() < deadline:
            status = request_status(timeout=1.5)
            if not status:
                print("[Monitor] No STATUS reply from ESP32")
                time.sleep(STATUS_INTERVAL)
                continue

            print_status(status, "Monitor")
            if status.get("armed") and not arm_confirmed:
                arm_confirmed = True
                print("[Monitor] Pixhawk accepted ARM. Arming checks passed at arm time.")
            if status.get("aborted"):
                print("\n[ERROR] Test mission aborted.")
                sys.exit(1)
            if status.get("completed"):
                print("\n[4] Done.")
                return
            time.sleep(STATUS_INTERVAL)
    except KeyboardInterrupt:
        print("\nCtrl+C received. Sending STOP/LAND...")
        reply = send("STOP", expect_reply=True)
        if reply:
            print(f"ESP32 reply: {reply}")
        sys.exit(130)

    print("\n[ERROR] Test mission monitor timed out.")
    sys.exit(1)

print("=" * 45)
print("  ESP32 Arm + Relay Test  (no takeoff)")
print("=" * 45)

print("\n[1] Uploading test mission...")
reply = send(json.dumps(MISSION, separators=(",", ":")), expect_reply=True)
if reply != "OK MISSION_STORED":
    print("[ERROR] ESP32 did not confirm mission storage.")
    print(f"        Reply: {reply or 'no UDP reply'}")
    sys.exit(1)
print("  <- ESP32 confirmed mission storage")

require_prestart_ready()

print("[2] Sending START...")
reply = send("START", expect_reply=True)
if reply != "OK START":
    print("[ERROR] ESP32 refused START.")
    print(f"        Reply: {reply or 'no UDP reply'}")
    sys.exit(1)
print("    Mission started on ESP32.\n")
print("    Expected sequence:")
print("    arm -> relay ON -> wait 5s -> relay OFF -> disarm\n")

monitor_mission()

print("    If arm was rejected, keep Pixhawk arming checks enabled and inspect Mission Planner messages.")
print("    Also verify SYSID_MYGCS = 255 and the Pixhawk serial port uses MAVLink.")
