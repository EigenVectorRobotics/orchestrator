#!/usr/bin/env bash
# One FVD sim run on vector-pc in tmux -L fvd: stack, bag, command; stops the bag and the stack after done or failed.
# A press takes the button pose from Thor's track_3d (start it first with thor_perception.sh), reached over
# ROS_STATIC_PEERS; button_source:=sim reads it from the sim instead.
# Usage (on vector-pc, from ~/eigenvector_ws/fvd_sim):
#   jean_dev/tool/hand_chaining/run_fvd.sh <run_name> [robot_start, e.g. [3.6,-3.5,-2.094]] [launch arguments, e.g. locomotion:=own]

RUN=$1
START=${2:-[3.6,-3.5,-2.094]}  # 2.5 m and 150 deg from the annotated start: GICP rejects the guess, the particle filter runs
EXTRA="${*:3}"
WS=$HOME/eigenvector_ws/fvd_sim
cd $WS
export ROS_STATIC_PEERS=100.94.122.122  # Thor on tailscale
tmux -L fvd kill-server 2>/dev/null
rm -rf runs/${RUN}_bag
STACK="source jean_dev/config/vector_pc.env && ros2 launch jean_dev/config/fvd_sim.launch.py map:=$WS/maps/sim_map_world_clean.pcd localization:=true executor:=true lio_config:=$HOME/eigenvector_ws/navigation/install/slam_elevatorlio/share/slam_elevatorlio/config/elevator_lio/root_unitree_g1.yaml places:=$WS/jean_dev/config/fvd_sim_places.yaml robot_start:=$START $EXTRA 2>&1 | tee runs/${RUN}_stack.log"
tmux -L fvd new-session -d -s run -n stack "bash -c '$STACK'"
for i in $(seq 120); do grep -q "waiting for a command" runs/${RUN}_stack.log 2>/dev/null && break; sleep 2; done
sleep 20
source jean_dev/config/vector_pc.env
ros2 param set /local_planner publish_free_paths true
TOPICS="/odom_base_link /state_estimation /sim/ground_truth /sim/state /sim/button_lit /sim/replay /sim/arm_plan /sim/arm_goal /path /free_paths /route_planner/path /way_point /goal_pose /navigation_cmd_vel /local_movement_cmd_vel /cmd_vel /goal_reached /route_planner/status /slam_elevatorlio/status /task_executor/status /task_executor/goal /task_executor/plan /initialpose /pf_relocalize/request /pf_relocalize/particles /pf_relocalize/status /door_state/landing /door_state/car /elevator/floor /registered_scan /camera/camera/color/image_raw/compressed /camera/camera/color/camera_info /feedback /tf /tf_static /speed /autonomy_mode"
# Thor's perception: the query and reset, the Cosmos seed box and its picture, EdgeTAM's boxes, mask points and pose
TOPICS="$TOPICS /route_goal /keyboard_input /track_3d/reset /init_bbox /seed_bbox/seed_overlay /track_3d/is_tracking /track_3d/detections_2d /track_3d/selected_target_pointcloud /track_3d/selected_normal_pose /sim/button_pose"
BAG="source jean_dev/config/vector_pc.env && ros2 bag record -s mcap -o runs/${RUN}_bag $TOPICS > runs/${RUN}_bag.log 2>&1"
tmux -L fvd new-window -t run -n bag "bash -c '$BAG'"
sleep 8
timeout 20 ros2 topic pub --once -w 1 /task_executor/command std_msgs/msg/String "{data: 'start floor:1; stop floor:2; target: door 1'}" > /dev/null
echo "command sent $(date +%T)"
for i in $(seq 600); do grep -qE "\] (done|failed):" runs/${RUN}_stack.log && break; sleep 2; done
sleep 6
grep -E "\] (done|failed):" runs/${RUN}_stack.log | tail -1
tmux -L fvd send-keys -t run:bag C-c
sleep 8
tmux -L fvd kill-server
ros2 daemon stop > /dev/null
echo "stopped $(date +%T)"
