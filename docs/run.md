# Run the robot

How to run the orchestrator on the Stanley G1: deploy it on the NUC, start it, run a task through the planner, run a single tree, bring a machine up by hand, stop, and watch a run. Each step gives the robot script from `mission_control/robot/` first (run from the laptop, acting on the NUC over ssh; `mission_control/robot/README.md`), then the manual command as the fallback. Which workspace and branch each machine runs is in [machines.md](machines.md); what to do when something goes wrong is in [troubleshooting.md](troubleshooting.md). The short on-site checklist is [demo.md](demo.md).

A Claude Code session that operates the robot does each step that starts, stops or moves something only on the operator's go.

## 1. Before you start

- The robot is powered and hangs in the gantry; the NUC, the Orin and the Thor are on and reach each other on the robot wire.
- The laptop is on Tailscale and has the agent bridge set up ([mission_control/README.md](../mission_control/README.md)) with a Claude Code login. Its Tailscale address is `agent_bridge.host` in `orchestrator/config/sites/stanley.yaml`; on another laptop, change it there and rebuild on the NUC (section 2).
- On the laptop, `ssh-add -l` lists your key (`deploy.sh` forwards your SSH agent).
- Foxglove on the laptop connects to `ws://vector-nuc:8765`, for relocalization and to watch the run (section 8).
- Open a terminal for the bridge and a shell for the robot scripts or the NUC, and the Run Viewer (`http://vector-nuc:8780`) in a browser; preflight starts it.
- Every ROS command on the NUC runs after `source ~/orchestrator_ws/env.sh` (the stack's environment with the orchestrator on top).
- Tests that publish or start nodes on the robot machines (a fake robot, a test tree on its own) use a free `ROS_DOMAIN_ID` (e.g. 99), never the live domain 0.

## 2. Deploy on the NUC

No script builds on the NUC (`start.sh` never builds). On the NUC:

```bash
cd ~/orchestrator_ws/src/orchestrator && git fetch && git checkout ltx-reorg && git pull   # the branch in use: machines.md
cd ~/orchestrator_ws && source env.sh && MAKEFLAGS=-j1 nice -n 19 ionice -c3 colcon build --parallel-workers 1 --packages-select skill_registry orchestrator
```

- Never build on the NUC while the robot stands: a build there once crashed the NUC, and the G1 fell. Build with one worker, as above.
- The install is a copy, not a symlink install. Contracts, the tree library (`skill_registry/trees/`), `skill_registry/preflight/preflight.xml`, the site file and the test trees are installed files: rebuild after changing any of them.
- What reads what, and when: the executor, the planner and the registry read the contracts and the places file at start; the planner reads the tree library and the preflight tree at start; `send_tree` reads the tree file you give it each time. So a contract, places, library or Python change needs a restart of the orchestrator (section 3); a test tree passed to `send_tree` by its source path needs nothing.
- The site file `orchestrator/config/sites/stanley.yaml` holds what a robot run needs beyond the code defaults: the places file, the planner backend, the agent bridge's address, the raw bag topics. Change the map, the operator laptop or the raw topics there, nowhere else.
- Run the tests from each package's own directory (`cd ~/orchestrator_ws/src/orchestrator/orchestrator && python3 -m pytest -q test`, the same in `skill_registry`): from the repository root, the import finds the installed package and the tests check the old code.
- Check a tree before sending it: `ros2 run skill_registry validate_tree <tree.xml>` (0 errors).
- A tree file can pull subtrees from other files with `<include path="subtrees/press.xml"/>`, relative to the file. `send_tree`, `validate_tree`, `planner_cli --tree` and the planner's tree library inline them before anything else reads the tree. Shared subtrees live in `skill_registry/trees/subtrees/`.

## 3. Start the orchestrator

From the laptop:

```bash
mission_control/robot/start.sh
```

It refuses when the tmux session `orchestrator` exists, empties `~/orchestrator.log`, starts the launch below in that session (`site:=$SITE`, `stanley` by default), and waits up to 60 s for the planner and the recorder. Then it prints the launch's first line, whether the executor is connected to the agent bridge (from `~/orchestrator.log`), and `ready`; otherwise the last lines of the log.

By hand, on the NUC:

```bash
tmux new -d -s orchestrator "bash -c 'source ~/orchestrator_ws/env.sh && ros2 launch orchestrator orchestrator.launch.py site:=stanley 2>&1 | tee ~/orchestrator.log'"
```

- It starts the registry, the executor, the recorder, the planner and the raw bag, with the values of `orchestrator/config/sites/stanley.yaml`: the cabin2 places file, `planner_backend: external` (a session or `planner_cli` answers the planner), the agent bridge's address, and the raw topics for SLAM and EKF replay, recorded to `~/bags/raw_<date>_<time>` until the launch stops (`raw_bag:=false` leaves it out).
- The first log line says what it took: `orchestrator: site stanley, places ..., planner external, agent bridge <address>`. A launch argument on the command line wins over the site (`places_file:=`, `planner_backend:=`, `planner_model:=`). Without `site`, the launch uses the code defaults: `orchestrator/config/places.yaml` (two placeholders, not enough for the elevator tree), the planner on the API, no agent bridge (agent skills fail), no raw bag. `fake:=true` is the laptop test with the fake robot.
- Ready when `~/orchestrator.log` shows `planner up: external ..., N existing trees` and the recorder's `recording to <session>`. The executor refuses trees until the recorder is ready (`record:=false` turns recording off), and task trees until a preflight has passed (section 5).
- The executor logs the bridge link to `~/orchestrator.log`: `connected to the agent bridge at <host>:<port>`, or `the agent bridge link dropped: <error>`. `start.sh` and `status.sh` read these lines; the bridge prints `executor connected from ...`.
- Parameters come from `orchestrator/config/orchestrator.yaml`; `params_file:=` gives another file. The planner's tree library is the installed `skill_registry/trees/` (parameter `tree_library`), and its preflight tree `skill_registry/preflight/preflight.xml` (`preflight_tree`; empty for none).
- A restart of the orchestrator ends the run in progress: cancel the tree first, and never restart while an agent call is open.

## 4. Run a task

The agent bridge on the laptop answers the agent calls, and `planner_cli` (or a session) answers the planner.

1. **Run the agent bridge**, yourself, in your own terminal (no script starts it: in the background it cannot answer `operator_confirm`). On the laptop, from a checkout of the branch the NUC runs:

   ```bash
   ~/.venvs/agent_bridge/bin/python mission_control/agent_bridge.py --site stanley
   ```

   Keep this window in view: the robot asks you questions here. A bridge on another `agent_spec.PROTOCOL` fails every call with "protocol mismatch". `--model` (default `claude-opus-5-5`) and `--effort` (default `low`) choose the model. It listens on the site's `agent_bridge` address, and the executor connects there (launched with the same site). The executor pushes each call with its evidence and frames over one TCP link. The model answers `press_judge` and `manipulation_recovery` in a few seconds, with the frames as images. `operator_confirm` rings the bell in the bridge window and waits for you to type `y` or `n`, then an optional reason. The record of each call is kept on the NUC, in the run's `agent/<call id>/` ([recorder.md](recorder.md)); with `--keep` the bridge also keeps each call in `~/agent_calls/<call id>/` on the laptop. The executor keeps each press judged pressed in `~/orch_state/press_references.jsonl` on the NUC (the old `~/.ros/orchestrator_press_references.jsonl` is read until the first new reference moves it over).
2. **Stand the robot up.** Stand the G1 in ready locomotion and remove the gantry: preflight does not stand the robot up, and stops the task if it is not in ready locomotion. Put the robot near the call panel on its floor.
3. **Send the task and answer the planner.** From the laptop:

   ```bash
   mission_control/robot/run_task.sh target_floor_1      # floor 2 to floor 1; target_floor_2 for the way back
   ```

   It refuses when the executor does not answer (run `start.sh`) or a tree runs (a new task halts it); `--force` as the second argument sends it anyway. Then it runs, on the NUC:

   ```bash
   ros2 run orchestrator planner_cli run --use-tree freight_elevator_go_to_place --param goal={target_floor_1}
   ```

   This sends the task, waits for the planning call while preflight runs (up to 15 min, `--timeout`), and answers it with the library tree and its parameters. `--task` and `--note` set the task sentence and the answer note; by default they name the tree and its parameters. To plan by hand instead (a new tree, or another choice), send the task yourself and use `planner_cli wait` and `answer` (step 5).
4. **Preflight runs first** (`skill_registry/preflight/preflight.xml`). It starts the Run Viewer and whichever of the NUC, Orin and Thor stacks are not up (`launch_*_stack`, over ssh from the NUC), checks for duplicate and stray nodes, checks SLAM (relocalizes in place, else asks to be relocalized), and checks that the G1 already stands in ready locomotion (`robot_ready`, loco FSM id 801). It never stands the robot up and never asks about the gantry, since `ready_locomotion` damps a standing robot first. The operator:
   - keeps the robot still while the NUC stack starts: SLAM measures the IMU offset in its first seconds;
   - sends `/initialpose` from Foxglove within 2 minutes when the robot says "Please relocalize me";
   - on "Task aborted: I am not in ready locomotion", stands the robot up and sends the task again (step 3).

   Right after the stacks are up, preflight stops when a robot-control node runs twice (`no_duplicate_nodes`) and warns about nodes started outside the launch and orphaned helpers (`no_stray_processes`). Run the same check by hand: `python3 -m orchestrator.stale_nodes report`.

   Preflight ends with a relocalization in place and "All nodes are running and my localization is healthy". If it fails, the planner does not plan; the trace says why.
5. **Planning by hand.** When `planner_cli run` is not used, send the task (`ros2 topic pub --once -w 1 /orchestrator/task std_msgs/msg/String "{data: '...'}"`), then in the planner session:

   ```bash
   ros2 run orchestrator planner_cli wait          # prints the call; the system prompt goes to ~/.ros/planner_system.md
   ros2 run orchestrator planner_cli answer --use-tree freight_elevator_go_to_place --param goal={target_floor_1} --note "To floor 1 by the elevator."
   ```

   `--use-tree` reuses a library tree (`--tree <file>` sends a new one); `--param` sets its parameters. `freight_elevator_go_to_place` needs only `goal`, a place on the other floor: the planner fills `goal_floor` from the place's z, and the tree derives the panels, the floor button and the door-out place from it. With no `--param` it rides from floor 1 to `{target_floor_2}`.
6. **During the ride** the robot calls the elevator, walks in, presses the floor button, rides, and walks to the goal. The bridge answers each call as it comes; watch the Run Viewer. If the robot asks you to press a button, press it. To stop at any time, cancel (section 7). A connected bridge has up to the call's contract timeout (`timeout_s` in `skill_registry/contracts/press_judge.yaml`, `manipulation_recovery.yaml` and `operator_confirm.yaml`). With no word from the bridge, the robot says so at 15 s and the call fails at 60 s (`UNREACHABLE_SAY_S`, `UNREACHABLE_FAIL_S` in `orchestrator/orchestrator/agent_push.py`); later calls fail at once until the bridge is heard from again ([troubleshooting.md](troubleshooting.md)). A failed call fails its agent skill, and the tree goes on as after any failure of that step (in the end, a person is asked to press).
7. **Results.** The run ends when the viewer shows `freight_elevator_go_to_place: success`. Results are under `~/orch_runs/<session>/runs/<n>-<tree>/` on the NUC: `tree.xml` (what ran), `records.jsonl`, `outcome.json`, the bag, and each agent call in `agent/<call id>/`. The session folder holds `events.jsonl`, the whole trace ([recorder.md](recorder.md)). The raw bag is in `~/bags/`.

## 5. Run one tree

On the NUC (no script sends a single tree):

```bash
ros2 run orchestrator send_tree <tree.xml>                 # add --blind for a tree that must run with no cloud
```

The executor runs a task tree only after a preflight passed since the launch: a task through the planner runs one first, and a preflight that fails or is cancelled closes the gate again. Before that, `send_tree` is refused ("no preflight has passed since the launch"); add `--skip-preflight` to run the tree anyway, which the trace marks (`preflight_skipped`). `preflight:=false` at launch turns the gate off, and the planner then runs no preflight before a task.

The bridge answers its agent calls, as in section 4. The recorder keeps the trace in `~/orch_runs/<session>/events.jsonl`.

Test trees, in `orchestrator/test_trees/`, to run a part of the task:

| Tree | Starts with the robot | Does |
|---|---|---|
| `freight_elevator_down_floor1_agentic.xml` | anywhere on floor 2, localized | the call panel, down, the door, the cabin, floor 1 |
| `freight_elevator_floor1_from_door_agentic.xml` | at the call panel, down pressed | waits for the door, walks in, floor 1 |
| `freight_elevator_floor1_press_agentic.xml` | at the inside place | EKF reset, floor 1 |
| `freight_elevator_floor1_walk_in_again_agentic.xml` | in the cabin, off the inside place by less than a reposition can fix | backs up 0.35 m behind the inside place, walks in, floor 1 |
| `freight_call_panel_press_repeat.xml` | anywhere on floor 2 | five cycles of walk in, down press, up press; the repeatability test below |
| `freight_elevator_floor_press_repeat.xml` | on floor 2, with the cabin held open there | five cycles of walk in, floor 1, 2 and 3 presses, walk out; one press per button, never retried |

The trees reset the EKF with `reset_ekf`, check the view after a reposition with `button_in_view`, and set the press offset before their first press: `press_button` refuses to run while no `set_press_offset` has run since the executor started.

### Repeatability test at the call panel

The test measures how often one press hits each call button, with no retries. Each cycle walks to the call panel, holds still for the EKF reset, presses the down button, then the up button, both with the right arm. The agent judges every press, and the tree goes on whatever the judge says.

1. Bring the NUC up with the cabin2 map (section 6) and check `"localized": true`. The Orin and the Thor run as usual.
2. Start the orchestrator (section 3) with `site:=stanley`.
3. Run the agent bridge on the laptop (section 4), then start the run: `ros2 run orchestrator send_tree ~/orchestrator_ws/src/orchestrator/orchestrator/test_trees/freight_call_panel_press_repeat.xml` (add `--skip-preflight` when no task has run a preflight since the launch).
4. The bridge judges each press. A press that move_to_pose refuses ("target out of reach") or that finds no target opens no call, so expect up to ten. A person by the panel notes the true outcome of each press, in order.

Compare the person's labels with the judge's decisions and the `press_judge` records in `~/orch_runs/<session>/runs/<n>-freight_call_panel_press_repeat/records.jsonl`. Each press record holds the capture (deepest point, sideways distance, `residual_m`) and the frame paths.

## 6. Bring a machine up by hand

Preflight starts the stacks itself (the `launch_nuc_stack`, `launch_orin_stack` and `launch_thor_stack` contracts in `skill_registry/contracts/`); no robot script does. When preflight cannot, start them one at a time and check each before the next.

**NUC** (robot interface, arm control, body EKF, navigation, `door_state_node`), with `scripts/deploy.sh` of the `eigen_vector` repository, from the laptop's `eigen_vector` checkout:

```bash
scripts/deploy.sh launch nuc map:=/home/vector/maps/two_floor_clean_cabin2.pcd localization:=true    # SSH_HOST=192.168.123.5 on the robot wire
```

It starts `bringup nuc.launch.py` in tmux on the NUC (`tmux -L eigen_vector`) and streams its log; Ctrl-C there stops the launch, so run it in the background. It is ready about 15 to 20 s later. On the NUC itself, `launch_nuc_stack` runs the same launch:

```bash
tmux -L eigen_vector new-session -d -s eigen_vector "bash -lc 'source ~/eigen_vector_ws/env.sh && LD_PRELOAD=\"\$EIGEN_VECTOR_PRELOAD\" ros2 launch bringup nuc.launch.py map:=/home/vector/maps/two_floor_clean_cabin2.pcd localization:=true 2>&1 | tee ~/eigen_vector_ws/launch.log'"
```

On 2026-11-08 the NUC ran two launch files instead, in this order, both only on the NUC ([machines.md](machines.md)): the robot interface and arm control, then navigation and SLAM. `door_state_node` was in neither, so it ran in its own session:

```bash
tmux -L eigen_vector new-session -d -s eigen_vector "bash -lc \"source ~/eigen_vector_ws/env.sh && LD_PRELOAD=\\\"\\$EIGEN_VECTOR_PRELOAD\\\" ros2 launch ~/nuc_manip_only.launch.py 2>&1 | tee ~/eigen_vector_ws/launch.log\""
tmux -L nav new-session -d -s nav "bash -lc \"source ~/eigen_vector_ws/env.sh && LD_PRELOAD=\\\"\\$EIGEN_VECTOR_PRELOAD\\\" ros2 launch ~/nuc_nav_only.launch.py map:=/home/vector/maps/two_floor_clean_cabin2.pcd localization:=true 2>&1 | tee ~/nav_launch.log\""
tmux new -d -s door_state "bash -c 'source ~/eigen_vector_ws/env.sh && export LD_PRELOAD=\$EIGEN_VECTOR_PRELOAD && ros2 launch elevator_states door_state_node.launch.py 2>&1 | tee ~/door_state.log'"
```

These can be stopped and started under a standing robot: `unitree_interface` sends no damp when it exits and starts in Idle, which does not command the motors (`mode_transitions.cpp`); the robot stays in Unitree's FSM 801 on its own. Release the arms first. Save `~/nav_launch.log` before a restart if a failure is to be examined: `tee` truncates it.

Poll, do not sleep:

```bash
ssh vector@vector-nuc 'source ~/eigen_vector_ws/env.sh; export LD_PRELOAD=$EIGEN_VECTOR_PRELOAD
  for i in $(seq 60); do s=$(timeout 3 ros2 topic echo --once --full-length /slam_elevatorlio/status | head -1)
    echo "$s" | grep -q "\"localized\": true" && break; sleep 2; done; echo "$s"
  timeout 5 ros2 topic hz /feedback | grep -m1 average            # about 1050 Hz: unitree_interface talks to the robot
  timeout 4 ros2 topic echo --once /unitree_interface/current_mode | grep name'
```

`/slam_elevatorlio/status` must say `"mode": "localization"` and `"localized": true`; a stack in mapping mode has no terrain graph and every goal aborts.

**Orin** (camera, `seed_bbox`, `track_3d`, `gripper_pose_estimator`): `scripts/deploy.sh launch orin` in the `eigen_vector` checkout, or on the Orin:

```bash
tmux -L eigen_vector new-session -d -s eigen_vector "bash -lc 'source ~/eigen_vector_ws/env.sh && LD_PRELOAD=\"\$EIGEN_VECTOR_PRELOAD\" ros2 launch bringup orin.launch.py 2>&1 | tee ~/eigen_vector_ws/launch.log'"
```

Ready when the new part of `~/eigen_vector_ws/launch.log` (after its `[INFO] [launch]` header) has `RealSense Node Is Up!`, `Track3D node active!` and `Camera info received` (about 15 s), and `/camera/camera/color/image_raw` publishes at about 6 Hz (from the NUC: `ros2 topic hz /camera/camera/color/image_raw`).

**Thor** (`ground_server` and the Cosmos daemon, then the VLM endpoint), on the Thor:

```bash
tmux -L eigen_vector_bringup new-session -d -s eigen_vector_bringup "bash -lc 'source ~/eigen_vector_ws_bringup/env.sh && ros2 launch bringup thor.launch.py 2>&1 | tee ~/eigen_vector_ws_bringup/launch.log'"
tmux new -d -s cosmos_vlm "env -i HOME=$HOME USER=$USER LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin bash --noprofile --norc -c 'source ~/eigen_vector_ws_bringup/env.sh && exec python3 ~/orchestrator_ws/src/orchestrator/orchestrator/orchestrator/cosmos_vlm_server.py --port 8090'"
```

The grounding is up when the log has `grounding service on /vlm_ground_server/ground_boxes` and `docker ps` shows `edgellm`; the VLM endpoint when `curl -s http://vector-thor:8090/v1/models` answers from the NUC. Both use the one Cosmos daemon, one request at a time. `launch_thor_stack` (preflight) runs the same two commands. `thor_up` checks only the grounding, so preflight passes without the VLM endpoint.

## 7. Stop

From the laptop:

```bash
mission_control/robot/cancel.sh     # halts the running tree; the orchestrator stays up
mission_control/robot/stop.sh       # steps 1 to 4 below; leaves the robot stacks up
```

`stop.sh` cancels the tree, publishes `/stop` 1 and `/autonomy_mode` false, releases the arms, goes to Idle, then stops the orchestrator with Ctrl-C (waiting up to 30 s) and closes its tmux session.

By hand, on the NUC:

1. Cancel the tree: `ros2 topic pub --once -w 1 /orchestrator/cancel std_msgs/msg/Empty "{}"`.
2. Stop the robot, each in its own command. Never chain them after a `pkill -f` whose pattern also matches your own shell.

   ```bash
   ros2 topic pub --once -w 1 /stop std_msgs/msg/Int8 "{data: 1}"
   ros2 topic pub --once -w 1 /autonomy_mode std_msgs/msg/Bool "{data: false}"
   ```

3. Release the arms and go to Idle:

   ```bash
   ros2 service call /unitree_interface/release_arms std_srvs/srv/Trigger "{}"
   ros2 service call /unitree_interface/change_mode unitree_interface_msgs/srv/ChangeControlMode "{requested_mode: 1}"
   ```

4. Stop the orchestrator with Ctrl-C in its session (`tmux send-keys -t orchestrator C-c`, then `tmux kill-session -t orchestrator` once the log ends): Ctrl-C lets the raw bag close its file.
5. Stop the stacks, when wanted: the NUC with `scripts/deploy.sh stop nuc` from the laptop's `eigen_vector` checkout (or `tmux -L eigen_vector send-keys -t eigen_vector C-c` on the NUC), the Orin and the Thor the same way in their tmux sessions, and `tmux kill-session -t cosmos_vlm` on the Thor.
6. Stop the bridge with Ctrl-C in its terminal.

The next tree clears `/stop` itself. Never use `ready_locomotion` on a standing robot: it damps the robot, then stands it up again.

## 8. Watch a run

- **From the laptop:** `mission_control/robot/status.sh` prints the executor's state (from `/orchestrator/status`, or `down`), the tmux sessions on the NUC, the last bridge link line of `~/orchestrator.log` (connected or dropped), and the last 15 lines of the log.
- **The Run Viewer** (`http://vector-nuc:8780`, started by preflight): the live tree and its decisions ([run_viewer/README.md](../run_viewer/README.md)).
- **The trace:** `/orchestrator/trace` (JSON) and `/orchestrator/log` (for the Foxglove Log panel); the recorder keeps it in the session's `events.jsonl`.
- **The bridge window:** each agent call, and the `operator_confirm` questions.
- **Foxglove** (`ws://vector-nuc:8765`), in the `map` frame: the current floor of the prior map `/route_planner/terrain` (latched), the live obstacles `/terrain_map` and `/terrain_map_ext` (colour by intensity), the candidate paths `/free_paths` (empty means all blocked), the scan `/registered_scan`, the door state `/door_state`. The places are not published by the stack; `~/places_markers.py` on the NUC publishes them as markers.
- **The camera and the track:** `/track_3d/tracked_overlay` in Foxglove shows the frame with the tracked target; `ros2 run tf2_ros tf2_echo foot_world selected_target` gives the target in `foot_world`.
