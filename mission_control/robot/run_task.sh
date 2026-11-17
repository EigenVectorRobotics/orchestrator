#!/usr/bin/env bash
# Ride the freight elevator to a place on the other floor: run_task.sh target_floor_1 (or target_floor_2, ...).
# Refuses while a tree runs, since a new task halts it (preflight is sent first). --force sends it anyway.
source "$(dirname "$0")/common.sh"

goal="${1:?usage: run_task.sh <place key, e.g. target_floor_1> [--force]}"
force="${2:-}"
state=$(executor_state)

case "$state" in
    down)    echo "refused: the executor does not answer (run start.sh)"; exit 1 ;;
    running) [ "$force" = --force ] || { echo "refused: a tree is running; cancel.sh first, or --force"; exit 1; } ;;
esac

echo "executor: $state; sending the task (preflight runs first, up to 15 min)"
nuc "$goal" <<'REMOTE'
source ~/orchestrator_ws/env.sh
ros2 run orchestrator planner_cli run --use-tree freight_elevator_go_to_place --param "goal={$1}"
REMOTE
