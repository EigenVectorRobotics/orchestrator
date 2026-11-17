#!/usr/bin/env bash
# Halt the running tree. The orchestrator stays up.
source "$(dirname "$0")/common.sh"

nuc <<'REMOTE'
source ~/orchestrator_ws/env.sh
ros2 topic pub --once -w 1 /orchestrator/cancel std_msgs/msg/Empty "{}"
REMOTE
echo "executor: $(executor_state)"
