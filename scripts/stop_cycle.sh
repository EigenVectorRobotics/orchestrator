#!/bin/bash
# Stop what start_cycle.sh started for RUN_DIR (the trace echo, agent_cli wait). It does not cancel
# the tree: for that, ros2 topic pub --once /orchestrator/cancel std_msgs/msg/Empty "{}" first.
R=${1:?usage: stop_cycle.sh RUN_DIR}
for p in wait echo; do [ -f $R/$p.pid ] && kill $(cat $R/$p.pid) 2>/dev/null; done
echo "stopped $R"
