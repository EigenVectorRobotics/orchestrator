#!/usr/bin/env bash
# Start the orchestrator on the NUC in tmux session "orchestrator" and wait until it is ready.
# Refuses when the session already exists. Does not build and does not start the robot stacks (preflight does).
source "$(dirname "$0")/common.sh"

nuc "$SITE" <<'REMOTE'
set -e
if tmux has-session -t orchestrator 2>/dev/null; then
    echo "refused: the orchestrator is already running (tmux session orchestrator)"; exit 1
fi
: > ~/orchestrator.log
tmux new -d -s orchestrator "bash -c 'source ~/orchestrator_ws/env.sh && ros2 launch orchestrator orchestrator.launch.py site:=$1 2>&1 | tee ~/orchestrator.log'"
for _ in $(seq 60); do
    if grep -q 'planner up' ~/orchestrator.log && grep -q 'recording to' ~/orchestrator.log; then
        grep -m1 'orchestrator: site' ~/orchestrator.log || true
        grep -q 'connected to the agent bridge' ~/orchestrator.log \
            && echo "agent bridge: connected" \
            || echo "agent bridge: NOT connected yet (start it on the laptop: mission_control/README.md)"
        echo "ready"; exit 0
    fi
    sleep 1
done
echo "not ready after 60 s; last lines of ~/orchestrator.log:"; tail -20 ~/orchestrator.log; exit 1
REMOTE
