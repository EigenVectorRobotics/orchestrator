#!/usr/bin/env bash
# Stop as in docs/run.md section 7: cancel the tree, stop the base, release the arms, go to Idle, then stop the
# orchestrator with Ctrl-C so the raw bag closes its file. The robot stacks stay up (deploy.sh stop nuc, ... for those).
source "$(dirname "$0")/common.sh"

nuc <<'REMOTE'
source ~/orchestrator_ws/env.sh
ros2 topic pub --once -w 1 /orchestrator/cancel std_msgs/msg/Empty "{}"
ros2 topic pub --once -w 1 /stop std_msgs/msg/Int8 "{data: 1}"
ros2 topic pub --once -w 1 /autonomy_mode std_msgs/msg/Bool "{data: false}"
ros2 service call /unitree_interface/release_arms std_srvs/srv/Trigger "{}"
ros2 service call /unitree_interface/change_mode unitree_interface_msgs/srv/ChangeControlMode "{requested_mode: 1}"

if tmux has-session -t orchestrator 2>/dev/null; then
    tmux send-keys -t orchestrator C-c
    for _ in $(seq 30); do tmux has-session -t orchestrator 2>/dev/null || break; pgrep -f 'orchestrator.launch.py' >/dev/null || break; sleep 1; done
    tmux kill-session -t orchestrator 2>/dev/null || true
    echo "orchestrator stopped"
else
    echo "orchestrator was not running"
fi
REMOTE
