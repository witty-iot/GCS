"""
Schema for the mission actions the ESP32 firmware actually understands (see
executeMission()'s action dispatch and stepSetMode()'s mode-name mapping in
Arduino_esp_code/esp_uploader.txt, and the "Supported Mission Actions" table
in the top-level README). The mission builder UI is generated from this, so
it can never build a step the firmware would silently reject or skip.

Each entry: action_name -> (description, [(field_name, kind, default), ...])
kind is one of: "float", "int", "choice:<comma-separated-options>"
"""

ACTIONS = {
    "arm": ("Arm the drone", []),
    "disarm": ("Disarm the drone", []),
    "takeoff": ("Switch to GUIDED, arm, and climb to an altitude", [
        ("alt", "float", 1.5),
    ]),
    "fly_to": ("Fly to an absolute lat/lon/alt (Guided position target)", [
        ("lat", "float", 0.0),
        ("lon", "float", 0.0),
        ("alt", "float", 2.0),
    ]),
    "reposition": ("Fly to an absolute lat/lon/alt (MAV_CMD_DO_REPOSITION)", [
        ("lat", "float", 0.0),
        ("lon", "float", 0.0),
        ("alt", "float", 2.0),
    ]),
    "local_ned": ("Fly to a North/East/Down offset from wherever the vehicle is, then hold", [
        ("x", "float", 0.0),
        ("y", "float", 0.0),
        ("z", "float", 0.0),
        ("seconds", "int", 5),
    ]),
    "brake_hover": ("Hold a zero-velocity Guided target for a duration", [
        ("seconds", "int", 5),
    ]),
    "wait": ("Hold the current step for a duration (sends no commands)", [
        ("seconds", "int", 5),
    ]),
    "set_mode": ("Switch ArduPilot flight mode", [
        ("mode", "choice:GUIDED,LOITER,RTL,LAND,AUTO,MANUAL", "GUIDED"),
    ]),
    "relay_on": ("Turn ESP32 GPIO4 relay ON", []),
    "relay_off": ("Turn ESP32 GPIO4 relay OFF", []),
    "rtl": ("Command Return To Launch", []),
    "land": ("Command LAND", []),
}


def format_params(step):
    return "  ".join(f"{k}={v}" for k, v in step.items() if k != "action")
