#!/usr/bin/env bash
# Ride an elevator to a place on the other floor: run_task.sh elevator1_target_floor_1 (or freight_target_floor_2, ...).
# The elevator is the first word of the place key.
# Refuses while a tree runs, since a new task halts it (preflight is sent first). --force sends it anyway.
source "$(dirname "$0")/common.sh"

goal="${1:?usage: run_task.sh <place key, e.g. elevator1_target_floor_1> [--force]}"
force="${2:-}"
state=$(executor_state)

case "$state" in
    down)    echo "refused: the executor does not answer (run start.sh)"; exit 1 ;;
    running) [ "$force" = --force ] || { echo "refused: a tree is running; cancel.sh first, or --force"; exit 1; } ;;
esac

echo "executor: $state; sending the task (preflight runs first, up to 15 min)"
nuc "$goal" <<'REMOTE'
source ~/orchestrator_ws/env.sh
ros2 run orchestrator planner_cli run --use-tree freight_elevator_go_to_place --param "elevator=${1%%_*}" --param "goal={$1}"
REMOTE
