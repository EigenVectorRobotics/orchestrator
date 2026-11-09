#!/bin/bash
# Run one tree with a session as the agent, as in the agent trial of 2026-11-04 (docs/RUNBOOK.md, "Run a tree").
#   start_cycle.sh [TREE.xml]      default: the whole task, freight_elevator_down_floor1_agentic.xml (installed copy)
# Starts in the background, each writing to ~/orch_runs/run_<HHMMSS>/: the trace (trace.txt), agent_cli wait
# (agent_wait.txt), the EKF-reset watcher (ekf_watch.txt) and send_tree (send_tree.txt). Prints the run folder.
# The watcher calls /body_ekf/reset each time the trace records the say of the trees' ekf_hold subtree
# ("Holding still to reset my balance estimate."); the tree holds 15 s there. Stop it all with stop_cycle.sh RUN_DIR.
source ~/orchestrator_ws/env.sh
TREE=${1:-$(ros2 pkg prefix skill_registry)/share/skill_registry/trees/freight_elevator_down_floor1_agentic.xml}
R=~/orch_runs/run_$(date +%H%M%S); mkdir -p $R
nohup ros2 topic echo /orchestrator/trace --field data > $R/trace.txt 2>&1 < /dev/null &
echo $! > $R/echo.pid
nohup ros2 run orchestrator agent_cli wait > $R/agent_wait.txt 2>&1 < /dev/null &
echo $! > $R/wait.pid
# the say text appears once per hold, in the record line ("inputs": {"text": "...estimate."}); not in the tree XML,
# where its quote is escaped
nohup bash -c "tail -n0 -F $R/trace.txt | grep --line-buffered -F \"Holding still to reset my balance estimate.\\\"\" | while read l; do echo \"\$(date +%T) trigger\"; ros2 service call /body_ekf/reset std_srvs/srv/Trigger | grep -o 'success=[A-Za-z]*'; done" > $R/ekf_watch.txt 2>&1 < /dev/null &
echo $! > $R/watch.pid
sleep 3
nohup ros2 run orchestrator send_tree "$TREE" > $R/send_tree.txt 2>&1 < /dev/null &
echo $R
