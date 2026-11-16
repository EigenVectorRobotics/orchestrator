# Operations

How to run the orchestrator on the Stanley G1: deploy it on the NUC, start it, run a task through the planner, run a single tree, bring a machine up by hand, and stop. Which workspace and branch each machine runs is in `docs/deployment.md`; what to do when something goes wrong is in `docs/troubleshooting.md`.

A Claude Code session that operates the robot does each step that starts, stops or moves something only on the operator's go.

## Before you start

- The robot is powered and hangs in the gantry; the NUC, the Orin and the Thor are on and reach each other on the robot wire.
- On the laptop, `ssh-add -l` lists your key (`deploy.sh` forwards your SSH agent).
- Every ROS command on the NUC runs after `source ~/orchestrator_ws/env.sh` (the stack's environment with the orchestrator on top).
- Tests that publish or start nodes on the robot machines (a fake robot, a test tree on its own) use a free `ROS_DOMAIN_ID` (e.g. 99), never the live domain 0.

## 1. Deploy the orchestrator on the NUC

```bash
cd ~/orchestrator_ws/src/orchestrator && git fetch && git checkout ltx-dev && git pull     # the branch in use: docs/deployment.md
cd ~/orchestrator_ws && source env.sh && colcon build --packages-select skill_registry orchestrator
```

- The install is a copy, not a symlink install. Contracts, the tree library (`skill_registry/trees/`), `preflight.xml` and the test trees are installed files: rebuild after changing any of them.
- What reads what, and when: the executor, the planner and the registry read the contracts and the places file at start; the planner reads the tree library and the preflight tree at start; `send_tree` reads the tree file you give it each time. So a contract, places, library or Python change needs a restart of the orchestrator (step 2); a test tree passed to `send_tree` by its source path needs nothing.
- Run the tests from each package's own directory (`cd ~/orchestrator_ws/src/orchestrator/orchestrator && python3 -m pytest -q test`, the same in `skill_registry`): from the repository root, the import finds the installed package and the tests check the old code.
- Check a tree before sending it: `ros2 run skill_registry validate_tree <tree.xml>` (0 errors).

## 2. Start the orchestrator

On the NUC:

```bash
tmux new -d -s orchestrator "bash -c 'source ~/orchestrator_ws/env.sh && ros2 launch orchestrator orchestrator.launch.py places_file:=/home/vector/maps/two_floor_clean_cabin2.places.yaml planner_backend:=external 2>&1 | tee ~/orchestrator.log'"
```

- It starts the registry, the executor, the recorder and the planner. `planner_backend:=external` makes a Claude Code session answer the planner (`planner_cli`); the launch default is `claude` (the API).
- Ready when `~/orchestrator.log` shows `planner up: external ..., N existing trees` and the recorder's `recording to <session>`. The executor refuses trees until the recorder is ready (`record:=false` turns recording off).
- No EKF-reset watcher is needed: a tree resets the body EKF itself with the `reset_ekf` skill (`/body_ekf/reset`, then it waits for the first torso pose after the 2 s still period). Do not start `~/watch_records_*.sh` beside a tree that calls `reset_ekf`, or the EKF is reset twice.
- Parameters come from `orchestrator/config/orchestrator.yaml`; `params_file:=` gives another file. The planner's tree library is the installed `skill_registry/trees/` (parameter `tree_library`), and its preflight tree `skill_registry/preflight/preflight.xml` (`preflight_tree`; empty for none).
- A restart of the orchestrator ends the run in progress: cancel the tree first, and never restart while an agent call is open.

## 3. Run a task through the planner

One session answers the agent calls, and the same or another session answers the planner. Only one session may answer agent calls: another `agent_cli wait` sees every call too (`pgrep -fa agent_cli`).

1. **The agent session** runs `ros2 run orchestrator agent_cli wait` in the background, and answers each call by its skill document in `skill_registry/agent_skills/`: `operator_confirm` (preflight's gantry question), `press_judge` (after every press), `manipulation_recovery` (a failed stance check, a failed press).
2. **Send the task:**

   ```bash
   ros2 topic pub --once -w 1 /orchestrator/task std_msgs/msg/String "{data: 'Take the freight elevator from floor 2 to floor 1.'}"
   ```

3. **Preflight runs first** (`skill_registry/preflight/preflight.xml`). It starts the Run Viewer and whichever of the NUC, Orin and Thor stacks are not up (`launch_*_stack`, over ssh from the NUC), checks SLAM (relocalizes in place, else asks to be relocalized), stands the G1 in ready locomotion in the gantry, and asks whether the gantry is removed. The operator:
   - sends `/initialpose` from Foxglove (`ws://vector-nuc:8765`) within 2 minutes when the robot says "Please relocalize me";
   - removes the gantry, then the session answers: `ros2 run orchestrator agent_cli decide confirmed --reason "Operator: gantry removed." --send`.

   Preflight ends with a relocalization in place. If it fails, the planner does not plan; the trace says why.
4. **The planner asks for a tree.** In the planner session:

   ```bash
   ros2 run orchestrator planner_cli wait          # prints the call; the system prompt goes to ~/.ros/planner_system.md
   ros2 run orchestrator planner_cli answer --tree FILE --note "What the robot will do and how it handles failures."
   ```

   **The tree library (`skill_registry/trees/`) is empty for now.** `freight_elevator_ride` was removed on 2026-11-16 (`539938b`): it used skills that `88016d2` removed (`track`, `button_in_reach`, `no_stale_publishers`). Until a library tree comes back, answer with `--tree FILE`. With a library tree, `--use-tree ID` reuses it with its default parameters, and `--param NAME=VALUE` (repeatable) sets others; a `<name>_floor` parameter is set from the floor of its place.

   The places and buttons for a ride from floor 1 to floor 2, for a tree that takes them as parameters: call panel preapproach `{freight_elevator_call_panel_preapproach_floor_1}` (not the call panel itself: a second walk to the place the robot stands on times out), call panel `{freight_elevator_call_panel_floor_1}`, call button `down button` (floor 1 has one grey call button below a black round light; the grounding calls the lower round button down and the light up), inside panel `{inside_freight_elevator_button_panel_floor_1}`, floor button `floor 2`, door-out place `{freight_elevator_door_out_floor_2}`, target `{target_floor_2}`.
5. **During the ride** the agent session answers each call as it comes. A call waits up to 600 s; then the agent skill fails and the tree goes on as after any failure of that step (in the end, a person is asked to press).
6. **Results** are under `~/orch_runs/<session>/runs/<n>-<tree>/`: `tree.xml` (what ran), `records.jsonl`, `outcome.json`, the bag. The session folder holds `events.jsonl`, the whole trace.

## 4. Run one tree without the planner

```bash
ros2 run orchestrator send_tree <tree.xml>                 # add --blind for a tree that must run with no cloud
```

Or, with the trace echo and `agent_cli wait` started beside it, each writing to `~/orch_runs/run_<HHMMSS>/`:

```bash
~/orchestrator_ws/src/orchestrator/scripts/start_cycle.sh <tree.xml>      # the tree is required
~/orchestrator_ws/src/orchestrator/scripts/stop_cycle.sh <run folder>    # when the tree has ended
```

`stop_cycle.sh` matters: an orphaned `ros2 topic echo` holds a DDS participant (N-001). Read progress with `grep -o '"text": "[^"]*"' <run folder>/trace.txt`.

Test trees, in `orchestrator/test_trees/`:

| Tree | Starts with the robot | Does |
|---|---|---|
| `call_elevator.xml` | anywhere, localized, with a place `call_panel` | walks to the call panel, presses up, waits for the door; runs blind (no cloud) |

The freight elevator test trees (`freight_elevator_down_floor1_agentic`, `freight_elevator_floor1_*_agentic`, `freight_call_elevator_*_agentic`, `call_elevator_agentic`, `freight_elevator_ride_agentic`) and the press repeatability trees (`freight_call_panel_press_repeat`, `freight_elevator_floor_press_repeat`) were removed on 2026-11-16 (`539938b`): they used skills that `88016d2` removed. Later commits of PR #8 (`fix/elevator-run-fixes`) bring back fixed versions of some of them.

## 5. Bring a machine up by hand

Preflight starts the stacks itself. When it cannot, start them one at a time and check each before the next.

**NUC** (robot interface, arm control, body EKF, navigation, `door_state_node`), from the laptop's `eigen_vector` checkout:

```bash
scripts/deploy.sh launch nuc map:=/home/vector/maps/two_floor_clean_cabin2.pcd localization:=true    # SSH_HOST=192.168.123.5 on the robot wire
```

It starts `bringup nuc.launch.py` in tmux on the NUC (`tmux -L eigen_vector`) and streams its log; Ctrl-C there stops the launch, so run it in the background. It is ready about 15 to 20 s later.

On 2026-11-08 the NUC ran two launch files instead, in this order, both only on the NUC (`docs/deployment.md`): the robot interface and arm control, then navigation and SLAM. `door_state_node` was in neither, so it ran in its own session:

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

**Orin** (camera, `seed_bbox`, `track_3d`): `scripts/deploy.sh launch orin`, or on the Orin:

```bash
tmux -L eigen_vector new-session -d -s eigen_vector "bash -lc 'source ~/eigen_vector_ws/env.sh && LD_PRELOAD=\"\$EIGEN_VECTOR_PRELOAD\" ros2 launch bringup orin.launch.py 2>&1 | tee ~/eigen_vector_ws/launch.log'"
```

Ready when the new part of `~/eigen_vector_ws/launch.log` (after its `[INFO] [launch]` header) has `RealSense Node Is Up!`, `Track3D node active!` and `Camera info received` (about 15 s), and `/camera/camera/color/image_raw` publishes at about 6 Hz (from the NUC: `ros2 run orchestrator agent_cli topics`).

**Thor** (`ground_server` and the Cosmos daemon, then the VLM endpoint), on the Thor:

```bash
tmux -L eigen_vector_bringup new-session -d -s eigen_vector_bringup "bash -lc 'source ~/eigen_vector_ws_bringup/env.sh && ros2 launch bringup thor.launch.py 2>&1 | tee ~/eigen_vector_ws_bringup/launch.log'"
tmux new -d -s cosmos_vlm "env -i HOME=$HOME USER=$USER LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin bash --noprofile --norc -c 'source ~/eigen_vector_ws_bringup/env.sh && exec python3 ~/orchestrator_ws/src/orchestrator/orchestrator/orchestrator/cosmos_vlm_server.py --port 8090'"
```

The grounding is up when the log has `grounding service on /vlm_ground_server/ground_boxes` and `docker ps` shows `edgellm`; the VLM endpoint when `curl -s http://vector-thor:8090/v1/models` answers from the NUC. Both use the one Cosmos daemon, one request at a time. `launch_thor_stack` (preflight) runs the same two commands.

## 6. Stop

1. Cancel the tree: `ros2 topic pub --once /orchestrator/cancel std_msgs/msg/Empty "{}"`.
2. Stop the robot: publish `/stop` 1 and `/autonomy_mode` false, each in its own command. Never chain them after a `pkill -f` whose pattern also matches your own shell.
3. Release the arms and go to Idle:

   ```bash
   ros2 service call /unitree_interface/release_arms std_srvs/srv/Trigger "{}"
   ros2 service call /unitree_interface/change_mode unitree_interface_msgs/srv/ChangeControlMode "{requested_mode: 1}"
   ```

4. Stop the orchestrator (`tmux kill-session -t orchestrator`) and any helpers (`stop_cycle.sh`); then the stacks: the NUC with `scripts/deploy.sh stop nuc` from the laptop (or `tmux -L eigen_vector send-keys -t eigen_vector C-c` on the NUC), the Orin and the Thor the same way in their tmux sessions, and `tmux kill-session -t cosmos_vlm` on the Thor.

The next tree clears `/stop` itself. Never use `ready_locomotion` on a standing robot: it damps the robot, then stands it up again.

## 7. Watch a run

- **The trace:** `/orchestrator/trace` (JSON) and `/orchestrator/log` (for the Foxglove Log panel); the recorder keeps it in the session's `events.jsonl`.
- **Foxglove** (`ws://vector-nuc:8765`), in the `map` frame: the prior map `/route_planner/terrain` (latched) or one floor of it on `/prior_map`, the live obstacles `/terrain_map` and `/terrain_map_ext` (colour by intensity), the candidate paths `/free_paths` (empty means all blocked), the scan `/registered_scan`, the door state `/door_state`. The places are not published by the stack; `~/places_markers.py` on the NUC publishes them as markers.
- **The camera and the track:** `ros2 run orchestrator agent_cli image overlay /tmp/o.jpg` shows the frame with the tracked target; `ros2 run tf2_ros tf2_echo foot_world selected_target` gives the target in `foot_world`.
