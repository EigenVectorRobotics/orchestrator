# Elevator demo

The G1 rides the freight elevator from one floor to a place on the other floor. One operator runs it from a laptop on Tailscale. The NUC code is assumed up to date and built.

## Before the demo

- The robot hangs in the gantry, powered. The NUC, Orin and Thor are on.
- The laptop has the agent bridge set up (`mission_control/README.md`) and a Claude Code login. Its Tailscale address is the `agent_bridge` host in `orchestrator/config/sites/stanley.yaml`; on another laptop, change it there.
- Foxglove on the laptop connects to `ws://vector-nuc:8765`, for relocalization.
- Open two terminals, the bridge and a shell on the NUC, and the Run Viewer in a browser.

## 1. Start the agent bridge (laptop)

```bash
~/.venvs/agent_bridge/bin/python mission_control/agent_bridge.py --site stanley
```

Keep this window in view. The robot asks you questions here.

## 2. Start the orchestrator (NUC)

```bash
tmux new -d -s orchestrator "bash -c 'source ~/orchestrator_ws/env.sh && ros2 launch orchestrator orchestrator.launch.py site:=stanley 2>&1 | tee ~/orchestrator.log'"
```

The site file gives the places, the planner backend, the bridge's address and the raw sensor bag, which records to `~/bags/raw_<date>_<time>` while the orchestrator runs. It is ready when the bridge prints `executor connected` and `~/orchestrator.log` shows `recording to <session>`.

## 3. Stand the robot up

Stand the G1 in ready locomotion and remove the gantry: preflight does not stand the robot up, and stops the task if it is not in ready locomotion. Put the robot near the call panel on its floor.

## 4. Send the task (NUC)

From floor 2 to floor 1:

```bash
source ~/orchestrator_ws/env.sh
ros2 run orchestrator planner_cli run --use-tree freight_elevator_go_to_place --param goal={target_floor_1}
```

It sends the task, waits through preflight and answers the planner with the library tree. From floor 1 to floor 2, use `goal={target_floor_2}`.

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
3. Stop the orchestrator and the raw bag with Ctrl-C, so the bag closes its file, then the session:

   ```bash
   tmux send-keys -t orchestrator C-c
   tmux kill-session -t orchestrator
   ```

4. Stop the bridge with Ctrl-C.

Results are in `~/orch_runs/<session>/runs/` on the NUC, the raw bag in `~/bags/`, and each agent call in its run's `agent/<call id>/`.
