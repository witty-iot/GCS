# Guided Mode Drift/Instability — Diagnosis

## Symptom

Flying manually via transmitter, the drone is very stable in Loiter and Land modes.
When flying autonomously via the ESP32 mission manager, the drone takes off to the
correct altitude in almost every mission script, but is never stable afterward — it
keeps drifting, even when the mission switches to Loiter mid-flight, and even in
mission scripts that stay in Guided mode the whole time
(`mission_brake_hover_test.py`, `mission_local_ned_test.py`).

GPS lock is always good (20+ satellites) and Pixhawk arming checks always pass.

## What the ArduPilot documentation says

Reviewed:

- [ac2_guidedmode.html](https://ardupilot.org/copter/docs/ac2_guidedmode.html) (user-level Guided Mode overview)
- [copter-commands-in-guided-mode.html](https://ardupilot.org/dev/docs/copter-commands-in-guided-mode.html) (developer reference for the MAVLink messages used to drive Guided mode)

Key points:

- **`GUID_TIMEOUT`** (default 3s) only applies to **velocity / acceleration / attitude**
  targets. If those stop arriving, the vehicle decelerates to a stop and hovers.
  **Position targets have no such re-send requirement** — once accepted, the vehicle
  holds that position until a new target arrives or the mode changes.
- The coordinate **frame** used with `SET_POSITION_TARGET_LOCAL_NED` /
  `SET_POSITION_TARGET_GLOBAL_INT` matters a lot:
  - `MAV_FRAME_LOCAL_NED` (1), `MAV_FRAME_GLOBAL_RELATIVE_ALT_INT` (6, etc.): position
    is relative to a **fixed** reference (EKF origin / home). Resending the *same*
    target repeatedly is safe and idempotent.
  - `MAV_FRAME_LOCAL_OFFSET_NED` (7), `MAV_FRAME_BODY_OFFSET_NED` (9): **"Positions
    are relative to the vehicle's current position"** — i.e. relative to wherever the
    vehicle *is* at the moment each message is processed, not relative to a fixed
    start point.
- Guided → Loiter mid-mission is a normal, supported mode change; the docs don't
  discourage mixing modes.

This frame distinction is the core of the diagnosis below.

## Root cause: offset-frame resend bug in `local_ned` / `brake_hover`

The ESP32 firmware on the `feature-work` branch (the branch that actually understands
the `local_ned` and `brake_hover` mission actions used by `mission_brake_hover_test.py`
and `mission_local_ned_test.py` — `master` doesn't have these actions at all) sends the
offset move using `MAV_FRAME_LOCAL_OFFSET_NED`, and then **re-sends the same offset on
the periodic waypoint-refresh timer**:

```cpp
// cmdLocalPositionTarget() — pkt[58] = 7  →  MAV_FRAME_LOCAL_OFFSET_NED

} else if (action == "local_ned") {
    ...
    if (stepStartTime == 0) {
      cmdLocalPositionTarget(x, y, z);          // first send: "go 5m north of HERE"
      stepStartTime = lastWpSendTime = millis();
    }
    if (millis() - lastWpSendTime >= WP_SEND_INTERVAL) {   // every 2000ms
      cmdLocalPositionTarget(x, y, z);          // re-send: "go 5m north of HERE" — but "HERE" has moved!
      lastWpSendTime = millis();
    }
```

Because the frame is offset-from-current-position, every retransmission is **not**
"keep going to the same spot" — it's a brand-new command meaning "go another 5 m
north / 2.5 m up from wherever you are right now." With `MOVE_SECONDS=4` /
`HOLD_SECONDS=8` and a 2s resend interval, the vehicle gets commanded to move again
2-4 times during a single "hold," each hop compounding the last. This is a runaway
drift generator, and it fires as the very first movement action after takeoff in both
`mission_brake_hover_test.py` and `mission_local_ned_test.py` — matching "takes off to
the correct height, then never stabilizes, keeps drifting."

`brake_hover`'s zero-velocity command is implemented correctly (frame 1,
`MAV_FRAME_LOCAL_NED`, resent every 500ms — fine per the 3s velocity-timeout rule),
but it runs *after* `local_ned` has already put the vehicle in an unknown, still-moving
state, so it's fighting momentum from a bug that already happened.

By contrast, `reposition` and `fly_to` in the same `feature-work` firmware use
`cmdGuidedPosition()` / `cmdReposition()` with **absolute global lat/lon**
(`SET_POSITION_TARGET_GLOBAL_INT`, `MAV_CMD_DO_REPOSITION`) — those are correctly
idempotent on resend and shouldn't drift.

### Fix applied

`local_ned` now converts the North/East/Down offset into an absolute lat/lon/alt
**once**, when the step starts (`offsetToLatLon()` in `esp_uploader.txt`), and resends
*that* fixed absolute target via `cmdGuidedPosition()` — the same safe, idempotent
`SET_POSITION_TARGET_GLOBAL_INT` path already used by `fly_to` / `reposition`. It also
now waits until the vehicle has actually arrived (haversine distance + altitude check)
before starting the hold timer, instead of blindly holding for a fixed duration
regardless of whether the vehicle got there. The dedicated
`cmdLocalPositionTarget()` function (the one that sent `MAV_FRAME_LOCAL_OFFSET_NED`)
was removed entirely so the unsafe pattern can't be reintroduced by a future caller.

`brake_hover`'s zero-velocity command was already implemented correctly (`MAV_FRAME_LOCAL_NED`,
velocity-only, resent every 500ms — well inside the 3s `GUID_TIMEOUT`) and didn't need to change.

## Secondary issues found

1. **`master` branch's `fly_to`** (used by `mission_uploader2.py` if running the
   `master` firmware) sends `MAV_CMD_NAV_WAYPOINT` (id 16) via `COMMAND_LONG`,
   repeated every 2s. This command is not part of ArduCopter's documented guided-mode
   command set (only `SET_POSITION_TARGET_*`, `SET_ATTITUDE_TARGET`, and
   `MAV_CMD_DO_REPOSITION` are). It's very likely silently ignored by the flight
   controller, meaning `fly_to` may just do nothing rather than navigate. Not itself a
   drift cause, but explains why the `feature-work` branch replaced it.

2. **Blocking `delay(300)` / `delay(500)` calls** inside the `arm` / `takeoff` mission
   steps stall the ESP32's main `loop()` for up to ~800ms at a time, during which it
   doesn't drain the Pixhawk UART or the MAVLink UDP bridge. This doesn't touch the
   Pixhawk's own control loop (that runs independently on the flight controller), but
   it can cause the ESP32 to miss/garble telemetry bytes and, in the worst case,
   mis-detect a stale heartbeat mid-mission.

## Why switching to Loiter mid-mission might still drift

This was the open question after the first pass: `local_ned`/`brake_hover` drifting in
pure Guided mode is now explained and fixed (above), but the plain
`mission_uploader.py` flow (Guided → takeoff → **Loiter** → wait → land) was also
reported as unstable, and Loiter is proven rock-solid when flown manually via
transmitter. Two independent explanations, and a way to tell them apart:

### 1. Every ArduCopter mode shares one EKF — mode switching can't fix a bad estimate

Loiter's position controller and Guided's position controller are not separate
"brains" with their own sensor fusion. Both read from the exact same place:
ArduPilot's single EKF (`NavEKF3`), which fuses GPS position/velocity, compass
(heading), barometer (altitude), and IMU (attitude, dead-reckoning between GPS
updates) into one "where am I / which way am I facing" estimate. Flight *mode* only
decides which control law turns "how far am I from where I should be" into motor
output — Loiter and Guided both ultimately hand a target to the same `AC_PosControl`
/ `AC_WPNav` position controller. Neither has any way to know the EKF's underlying
estimate is wrong.

So if the EKF's **input** is corrupted, every mode that holds position — Loiter,
Guided-idle-hold, even RTL — will drift or hunt identically, because they're all
being fed the same wrong "truth." Switching modes changes nothing about that input.

**Mechanisms specific to a relay-driven pump/sprayer** (the relay is wired only to the
ESP32, not the Pixhawk, but it very likely shares the airframe, wiring runs, and
possibly the battery/BEC with the flight controller):

- **Compass interference.** Any current-carrying wire near the compass creates a
  magnetic field proportional to current (Biot–Savart law). A pump motor's steady-state
  and inrush current, if routed near the flight controller or an external
  compass/GPS module, biases the measured magnetic field. The EKF then computes a
  biased heading (yaw) estimate. Even a few degrees of yaw error causes the position
  controller to apply corrective velocity in a direction that doesn't match reality —
  which looks exactly like "drift that Loiter can't fix," because Loiter is faithfully
  holding position *relative to a wrong heading*.
- **Vibration.** An unisolated pump motor injects vibration into the airframe, often in
  the frequency band the accelerometers/gyros are most sensitive to. This raises the
  EKF's velocity/position innovation variances (noisier, laggier estimate), which shows
  up as small oscillations or drift that never quite settles, in any mode.
- **Power-rail interaction.** If the relay coil or pump motor draws current from the
  same battery/BEC that powers the flight controller and servo rail without adequate
  separation/filtering, switching transients can cause brief voltage sag or noise on
  that rail, glitching the IMU or other peripherals right when the relay switches.

**Why "20 satellites" and "arming checks passed" don't catch this:** satellite count
only reflects GPS *receiver* quality, not whether the fused EKF output is being
corrupted downstream by compass/vibration noise. ArduPilot's arming checks are mostly
static, pre-flight checks (compass health at rest, EKF initialized, compass-vs-GPS
heading agreement) — they don't continuously monitor compass/EKF health *while a pump
is actively drawing current mid-flight*.

**Concrete things to check/try, roughly in order of effort:**

1. Pull the dataflash `.bin` from an affected flight and look at `MAG` (raw compass
   field), `VIBE` (vibration), and `XKF4` (EKF innovation variances — `SM` is the
   magnetometer variance, `SV`/`SP` are velocity/position variance) right at the moment
   `relay_on` fires. A step change in `MAG` or a jump in `XKF4.SM` at that exact
   timestamp would confirm compass interference; a jump in `VIBE` would point at
   vibration instead.
2. Physically move the relay wiring / pump power leads as far from the compass and
   flight controller as practical, and twist the pump's positive/return leads together
   (cancels the magnetic field from the current loop).
3. If using an external compass, make sure it's mounted well away from the relay/pump
   wiring — this is the standard fix for this exact class of problem.
4. Add a flyback diode across the relay coil if not already present, and consider a
   separate BEC/battery tap for the pump rather than sharing the flight controller's
   supply.
5. As a software-side mitigation (only if wiring fixes aren't enough): ArduPilot
   supports `COMPASS_MOTCT`/throttle-based compass motor compensation, and in
   persistent-interference cases some builds move to GPS-based yaw (moving baseline /
   dual antenna) instead of relying on the compass at all.

### 2. The mode-change command itself (now upgraded)

Separately — and this applies with or without the relay — the firmware was sending
mode changes using the legacy MAVLink1 `SET_MODE` message (id 11), deprecated for
years in favor of `MAV_CMD_DO_SET_MODE` via `COMMAND_LONG`. Both are still accepted by
ArduPilot, so this was unlikely to be *the* cause, but it's the less-tested path and
not what current ArduPilot tooling (Mission Planner, QGroundControl, MAVSDK) actually
sends. The firmware has been updated to send mode changes via `MAV_CMD_DO_SET_MODE`
(see `cmdSetMode()` in `esp_uploader.txt`) as good practice and to rule this out.

### Telling the two apart: the new comparison scripts

`Missions/mission_hover_guided_test.py` and `Missions/mission_hover_loiter_test.py` fly
the *identical* profile (arm → takeoff to 1.5m → hold 5s → land) with **no relay
involved at all** — the only difference is that one holds in Guided and the other
switches to Loiter for the hold:

- **Only the Loiter flight drifts →** points at the mode transition or Loiter-specific
  behavior (rules out the relay/EMI hypothesis for this specific case, since no relay
  is used).
- **Both drift by a similar amount →** points at something common to both modes — most
  likely the EKF/position-estimate quality itself (which could still be
  airframe-specific: vibration from the frame/props, a marginal compass mount, magnetic
  interference from ESCs/power wiring even without the relay, etc.), not anything
  mode-specific in the firmware.
- **Neither drifts →** the relay/pump *is* the dominant cause for the missions that
  drifted before, and the checklist above applies.

Run them back-to-back, in similar wind/GPS conditions, and compare the `STATUS` output
(`lat`/`lon`/`alt` over time) or better, the dataflash log from each flight.

## What the flight log in the repo shows

Checked `new log.log` (a decoded ArduPilot dataflash log, ~1M lines). It contains
multiple unrelated sessions concatenated together — manual RC Stabilize/Loiter flying
(including one real crash-disarm event around t=256s from `AngErr=156°` / near-freefall
accel, unrelated to the ESP32 missions), plus separate Auto/RTL mission testing using
a native ArduPilot mission with a `SetRelay` item (not the ESP32 JSON mission system).

The two short Guided-mode arm cycles that could be isolated (~86-107s and ~108-122s)
both show healthy EKF variances (`XKF4` fields < 0.06), low vibration (`VIBE` < 1.5),
and near-zero position-controller tracking error (`PSCN`/`PSCE`: desired ≈ target ≈
actual throughout). These look like the no-takeoff bench test
(`mission_uploader_test.py`), not a flight exhibiting the reported drift.

**This log does not capture the specific bad flight being diagnosed.** To confirm the
`local_ned` offset-resend theory empirically, capture a fresh `.bin` log from a run
that reproduces the drift and inspect:

- `PSCN` / `PSCE` (target vs. actual North/East position) for a stepped jump every
  ~2 seconds during the `local_ned` step
- `XKF4` (EKF variances) and `VIBE` (vibration) around the same window, to rule out
  an EKF/vibration-driven cause instead

## Guided vs. Loiter — recommendation

Don't treat it as either/or:

- Use **Guided** for legs that need active navigation (takeoff, fly-to/reposition with
  absolute coordinates) — that's what it's for, and the docs don't discourage it.
- For "just hold position for N seconds," switching to **Loiter** (as
  `mission_uploader.py` already does) is reasonable and arguably safer, since it's
  ArduPilot's own well-tested position-hold controller rather than custom
  guided-setpoint bookkeeping in the ESP32 firmware.
- Loiter only helps if the underlying EKF estimate is clean — it won't mask either the
  offset-resend bug or an EMI-corrupted position estimate.

**Status:** the `local_ned` resend bug is fixed (see "Fix applied" above; firmware is
now merged into `master`, which is the repo's only branch). The Loiter mode-switch
drift question is not yet conclusively diagnosed — it needs either a dataflash log from
an affected flight, or a run of the two comparison scripts described above
(`mission_hover_guided_test.py` / `mission_hover_loiter_test.py`) to tell the EMI
hypothesis apart from a mode-transition-specific cause.
