# Elevator demo

The G1 rides the freight elevator from one floor to a place on the other floor. One operator runs it from a laptop on Tailscale. The NUC code is assumed up to date and built.

## Before the demo

- The robot hangs in the gantry, powered. The NUC, Orin and Thor are on.
- The laptop has the agent bridge set up (`mission_control/README.md`) and a Claude Code login.
- Foxglove on the laptop connects to `ws://vector-nuc:8765`, for relocalization.
- Open three terminals: the bridge, a shell on the NUC, and the Run Viewer in a browser.

## 1. Start the agent bridge (laptop)

```bash
~/.venvs/agent_bridge/bin/python mission_control/agent_bridge.py
```

Keep this window in view. The robot asks you questions here.

## 2. Start the orchestrator (NUC)

```bash
source ~/orchestrator_ws/env.sh
tmux new -d -s orchestrator "bash -c 'source ~/orchestrator_ws/env.sh && ros2 launch orchestrator orchestrator.launch.py places_file:=/home/vector/maps/two_floor_clean_cabin2.places.yaml planner_backend:=external 2>&1 | tee ~/orchestrator.log'"
```

It is ready when the bridge prints `executor connected` and `~/orchestrator.log` shows `recording to <session>`.

## 3. Record the raw sensors (NUC)

```bash
tmux new -d -s rawbag "bash -c 'source ~/eigen_vector_ws/env.sh && LD_PRELOAD=\"\$EIGEN_VECTOR_PRELOAD\" ros2 bag record -o ~/bags/raw_\$(date +%Y%m%d_%H%M) /lidar/scan /imu/data /state_estimation /slam_elevatorlio/status /slam_elevatorlio/floor /tf /tf_static /odom_base_link /LIO/odom /LIO/set_elevator_flag /LIO/imu_acc_offset'"
```

## 4. Send the task (NUC)

First stand the G1 in ready locomotion and remove the gantry: preflight does not stand the robot up, and stops the task if it is not in ready locomotion. Put the robot near the call panel on its floor. To go from floor 2 to floor 1:

```bash
ros2 topic pub --once -w 1 /orchestrator/task std_msgs/msg/String "{data: 'Take the freight elevator to target_floor_1.'}"
ros2 run orchestrator planner_cli wait
ros2 run orchestrator planner_cli answer --use-tree freight_elevator_go_to_place --param goal={target_floor_1} --note "To floor 1 by the freight elevator."
```

From floor 1 to floor 2, use `target_floor_2` in both places.

## 5. Preflight

Preflight runs before the ride. It starts any stack that is down, then the Run Viewer at `http://vector-nuc:8780`, checks localization, and checks that the robot stands in ready locomotion; it does not stand the robot up or ask about the gantry. Your part:

1. Keep the robot still while the NUC stack starts. SLAM measures the IMU offset in its first seconds.
2. If the robot says "Please relocalize me", send `/initialpose` from Foxglove within 2 minutes.
3. If the robot says "Task aborted: I am not in ready locomotion", stand it up and send the task again (step 4).

Preflight ends with "All nodes are running and my localization is healthy".

## 6. The ride

The robot calls the elevator, walks in, presses the floor button, rides, and walks to the goal. The bridge answers each press check and recovery on its own. Watch the Run Viewer.

- If the robot asks you to press a button, press it.
- To stop at any time:

  ```bash
  ros2 topic pub --once /orchestrator/cancel std_msgs/msg/Empty "{}"
  ```

The run ends when the viewer shows `freight_elevator_go_to_place: success`. For the way back, repeat step 4 with the other goal.

## 7. Stop

1. Cancel any running tree (step 6).
2. Stop the robot and release the arms as in `operations.md` section 6.
3. Stop the bag and the orchestrator:

   ```bash
   tmux kill-session -t rawbag
   tmux kill-session -t orchestrator
   ```

4. Stop the bridge with Ctrl-C.

Results are in `~/orch_runs/<session>/runs/` on the NUC, the raw bag in `~/bags/`, and each agent call in its run's `agent/<call id>/`.
