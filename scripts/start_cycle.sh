#!/bin/bash
# Run one tree with a session as the agent (docs/operations.md, "Run one tree without the planner").
#   start_cycle.sh [TREE.xml]      default: the whole task, freight_elevator_down_floor1_agentic.xml (installed copy)
# Starts in the background, each writing to ~/orch_runs/run_<HHMMSS>/: the trace (trace.txt), agent_cli wait
# (agent_wait.txt) and send_tree (send_tree.txt). Prints the run folder. Stop it all with stop_cycle.sh RUN_DIR.
source ~/orchestrator_ws/env.sh
TREE=${1:-$(ros2 pkg prefix orchestrator)/share/orchestrator/test_trees/freight_elevator_down_floor1_agentic.xml}
R=~/orch_runs/run_$(date +%H%M%S); mkdir -p $R
nohup ros2 topic echo /orchestrator/trace --field data > $R/trace.txt 2>&1 < /dev/null &
echo $! > $R/echo.pid
nohup ros2 run orchestrator agent_cli wait > $R/agent_wait.txt 2>&1 < /dev/null &
echo $! > $R/wait.pid
sleep 3
nohup ros2 run orchestrator send_tree "$TREE" > $R/send_tree.txt 2>&1 < /dev/null &
echo $R
