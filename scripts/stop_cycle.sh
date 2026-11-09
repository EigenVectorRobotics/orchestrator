#!/bin/bash
# Stop what start_cycle.sh started for RUN_DIR (the trace echo, agent_cli wait, the EKF watcher). It does not cancel
# the tree: for that, ros2 topic pub --once /orchestrator/cancel std_msgs/msg/Empty "{}" first.
R=${1:?usage: stop_cycle.sh RUN_DIR}
for p in watch wait echo; do [ -f $R/$p.pid ] && kill $(cat $R/$p.pid) 2>/dev/null; done
pkill -f "tail -n0 -F $R/trace.txt"      # the watcher's tail, which outlives its bash
echo "stopped $R"
