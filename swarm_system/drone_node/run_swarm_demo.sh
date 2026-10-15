#!/usr/bin/env bash
# run_swarm_demo.sh
# -------------------
# ONE command to fly the whole swarm_mission.xml, for demo/SITL use.
#
# Real deployment note: on actual hardware there is no single "run the
# swarm" command — each drone has its own Jetson running its own
# drone_node process, started independently when that Jetson boots.
# This script exists purely to make demoing multiple SITL drones on one
# laptop convenient: it launches one drone_node process per drone config
# listed below, all pulling from the same fleet_manager/missions/swarm_mission.xml
# via normal registration (not a shortcut, not a different code path).
#
# Usage:
#   ./run_swarm_demo.sh
#
# Add more drones by adding another config file + line below.
set -e

cd "$(dirname "$0")"

CONFIGS=(
  "config/drone_config_1.yaml"
  "config/drone_config_2.yaml"
)

PIDS=()

cleanup() {
  echo ""
  echo "Stopping all drones..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

for cfg in "${CONFIGS[@]}"; do
  echo "Launching drone_node with $cfg ..."
  python3 main.py --config "$cfg" --run-now &
  PIDS+=($!)
done

echo ""
echo "All drones launched (PIDs: ${PIDS[*]}). Ctrl-C to stop all."
wait
