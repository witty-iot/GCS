# GCS_UI

Small desktop control panel for the ESP32/Pixhawk mission bridge. Talks the
exact same UDP JSON mission protocol (`192.168.4.1:14551`) that
`Missions/mission_hover_guided_test.py` and `mission_hover_loiter_test.py`
use — see the top-level README's "Mission Command Protocol" section. Nothing
under `Missions/` or `Arduino_esp_code/` is modified by this app; it's just
another client of the existing protocol (`flight_logger.py` is imported and
reused as-is for logging, not changed).

Run it:

```bash
uv run python GCS_UI/app.py
```

No new dependencies — built on `tkinter`, which ships with Python.

## Tabs

- **Mission Builder**: pick an action (`arm`, `takeoff`, `fly_to`, `local_ned`,
  `set_mode`, `wait`, `land`, ...), fill in its parameters, add it to the step
  list, reorder/remove as needed, then **Upload Mission** and **Start
  Mission**. The action list and parameters are generated from
  `mission_actions.py`, which mirrors exactly what the ESP32 firmware's
  `executeMission()` dispatch understands — the UI cannot build a step the
  firmware would silently skip. "Quick commands" (ARM/DISARM/RTL/STOP) send
  a one-step mission immediately, for fast bench checks.
- **Status / Telemetry**: live `STATUS` readout, polled once a second —
  armed, GPS fix/sats, altitude, lat/lon, mode, mission step, and Pixhawk's
  own PreArm/reject reason (`last_important_text`, survives routine GPS/EKF
  chatter — see top-level README).
- **Logs**: a live console for this session (script actions + every Pixhawk
  STATUSTEXT), plus a browser for past `logs/*.log` files written by any
  script or by this app. One detailed log is written per app session (via
  the same `FlightLogger` the CLI scripts use, listening on the dedicated
  `14552` logger port) and finalized when the window closes.
- **Run Scripts**: lists `.py` files in `Missions/` (or Browse to any other
  script), runs the selected one as a subprocess with optional extra CLI
  args, and streams its stdout/stderr live into a console pane. One script
  at a time; Stop terminates it.

## Notes

- Bench-test safety: **never** run `arm`/`takeoff` without propellers removed
  and the vehicle restrained — ArduPilot's altitude controller cannot tell
  it isn't climbing and will keep raising throttle. See the top-level
  README's Safety Behavior section.
- `set_mode` only offers `GUIDED`/`LOITER`/`RTL`/`LAND`/`AUTO`/`MANUAL` —
  the only mode names the firmware's `stepSetMode()` recognizes.
- If UDP `14552` can't be opened (e.g. Mission Planner holding a shared
  port), the session log still gets written with the app's own action
  notes, just without the raw Pixhawk message capture — same caveat as the
  CLI scripts.
