# Elevator button-press test runbook (Stanley G1)

How to run the walk-and-press test at the freight elevator, judge each press, and decide the next step. It collects what we learned on 2026-11-04, in the field test and in the agent trial that night (the whole task run by the orchestrator, with a Claude Code session as the agent; "Agent trial, 2026-11-04" below, and `skill_registry/agent_skills/manipulation_recovery.experience.md` E-014 to E-020).

## Setup

- **Machines:** the NUC runs the robot interface, arm control, body EKF and navigation. The Orin runs the camera, `seed_bbox` and `track_3d`. The Thor runs Cosmos grounding.
- **Access and workspaces** (seen 2026-11-04). Each machine holds several workspaces; the one in the table is the one its running nodes come from. Passwords are not kept in this file: ask the team.
  - **Reaching the Orin and the Thor (agent trial).** From the laptop, `ssh -J vector@vector-nuc unitree@192.168.123.164` timed out in the banner exchange, while the NUC itself reaches the Orin at once with its own key: run Orin commands as `ssh vector@vector-nuc "ssh unitree@192.168.123.164 '...'"`. `vector-thor` (tailscale) timed out from the laptop; the robot wire worked: `ssh -o HostKeyAlias=vector-thor -J vector@vector-nuc vector@192.168.123.1` (password). A session without `sshpass` can give the password with `SSH_ASKPASS` pointing at a script that prints it, `SSH_ASKPASS_REQUIRE=force`.

  | Machine | SSH | OS, ROS | Workspace in use | Notes |
  |---|---|---|---|---|
  | NUC | `ssh vector@vector-nuc` (`192.168.123.5` on the robot wire) | Ubuntu 24.04, Jazzy | `~/eigen_vector_ws` | `env.sh` there is written by `deploy.sh`. Other workspaces (`eigen_vector_ws_lio`, `eigen_vector_ws_apt`, `manip_test*`, `vector_robotics`) are not in use |
  | Orin | `ssh unitree@unitree-g1-nx` | Ubuntu 22.04, Humble | `~/eigen_vector_ws` | `seed_bbox` and `track_3d` run from its install; the camera is the apt `realsense2_camera`, raw RGB8 only (no compressed transport installed) |
  | Thor | `ssh vector@vector-thor` | Ubuntu 24.04, Jazzy | `~/eigen_vector_ws_bringup` | `ground_server` runs from it (vlm `main` @ `3ca4861`), not from `~/eigen_vector_ws`. The Cosmos daemon runs in the docker container `edgellm` |
- **Launch:** from the laptop master repo, `eigen_vector/scripts/deploy.sh launch nuc map:=/home/vector/maps/two_floor_clean_cabin2.pcd localization:=true`. Use `SSH_HOST=192.168.123.5` when the laptop is on the robot wire.
- **Check localization first:** `/slam_elevatorlio/status` must say `"mode": "localization"` and `"localized": true`. A stack in mapping mode has no terrain graph, and every nav goal aborts. The robot must start near the map origin, or if the first match is rejected (`structure inlier ratio` below 0.80, falling): ask the user to send a pose estimate on `/initialpose`, e.g. from Foxglove (`ws://vector-nuc:8765`, the NUC bridge).
  - **Check a match yourself** (agent trial): `python3 ~/orchestrator_ws/src/orchestrator/scripts/scan_fit.py` in the robot environment. It compares `/registered_scan` with the prior map: a right match has a median about 3 cm, 75 to 80 % of points within 10 cm, and the share falling when the scan is turned 5 or 10 deg either way. Both launches that night matched on their own at yaw -30.1 and -25.7 deg, near the edge of the +-30 deg window, and both passed this check (0.76 and 0.80 within 10 cm at 0 deg, 0.56 to 0.66 at +-10 deg), so no `/initialpose` was needed; a later check in the cabin gave 0.79.
- **Places:** `~/maps/two_floor_clean_cabin2.places.yaml` on the NUC (the map two_floor_clean_cabin2.pcd: two_floor_clean.pcd without the stray surfaces in the floor-1 cabin, same frame) has annotations of the map. The inside place sits about 10 cm back from the panel (x 3.410, y -0.299, yaw 152.8 deg; until 2026-11-08 x 3.428, y -0.263, moved 4 cm to the robot left for panel reach). The call panel place moved 6 cm forward along its yaw the same day (x 1.829, y -1.372, was 1.771, -1.356). The floor 1 places (`... floor 1`) take the same x y with their own z; backups `~/maps/two_floor_clean_cabin2.places.yaml.bak_20261108_*`. These stances are soft, a few centimetres either way: check them against `button_in_reach` and the press results, and tune.
- **Code state:** the NUC runs `manipulation_stack` on `test/pelvis-correction` plus uncommitted edits (`final_leg_steps`, `final_leg_settle_time`); The NUC `navigation_stack` has the uncommitted path_follower fix and `goal_clear_range` 0.1.

## Bring-up for an agent test

The orchestrator runs a tree, and its `manipulation_recovery` agent skill is answered by a Claude Code session with `agent_cli` (see `skill_registry/agent_skills/manipulation_recovery.md`). Bring the machines up in this order, one at a time, and check each before the next. A session does each step only on the operator's go.

1. **NUC stack** (robot interface, arm control, body EKF, navigation): from the laptop's `eigen_vector` checkout, `scripts/deploy.sh launch nuc map:=/home/vector/maps/two_floor_clean_cabin2.pcd localization:=true` (`ssh-add -l` first: it forwards your SSH agent). It starts `bringup nuc.launch.py` in tmux on the NUC and streams its log; keep that command running, since Ctrl-C there stops the launch. Check localization as in Setup.
   - **Poll, do not sleep.** The stack is ready about 15 to 20 s after launch: poll `/slam_elevatorlio/status` until `"localized": true`, not a fixed wait. From the laptop, in one command (the NUC needs the robot DDS environment for every ROS call):

     ```bash
     ssh vector@vector-nuc 'source ~/eigen_vector_ws/env.sh; export LD_PRELOAD=$EIGEN_VECTOR_PRELOAD
       for i in $(seq 60); do s=$(timeout 3 ros2 topic echo --once --full-length /slam_elevatorlio/status | head -1)
         echo "$s" | grep -q "\"localized\": true" && break; sleep 2; done; echo "$s"
       timeout 5 ros2 topic hz /feedback | grep -m1 average            # about 1050 Hz: unitree_interface is talking to the robot
       timeout 4 ros2 topic echo --once /unitree_interface/current_mode | grep name'
     ```

     `deploy.sh launch` streams the launch log until Ctrl-C; run it in the background and check with the poll above. To stop the stack: `scripts/deploy.sh stop nuc` (it waits up to 30 s for the launch to exit). With the laptop on the robot wire, prefix `SSH_HOST=192.168.123.5`; `vector-nuc` resolves to the tailscale address.
   - **No robot link.** If `/feedback` is silent or the NUC cannot ping `192.168.123.161` (the robot) and `192.168.123.164` (the Orin), check the cable: `cat /sys/class/net/enp86s0/carrier` is 1 with a link, `ip -br a show enp86s0` shows `192.168.123.5`. The NUC reboots with the robot when the battery is changed; after that, every machine's stack must be started again. `~/eigen_vector_ws/launch.log` keeps the previous run's lines until `tee` truncates it, so a grep for a ready line can match the old run: check its first line first.
   - **DDS participant limit.** The NUC uses unitree_sdk2's CycloneDDS 0.10.2, which gives each process an automatic participant index from a limited range. The stack alone runs about 25 processes; with the executor, `agent_cli`, the ros2 daemon and a few CLI calls, new processes failed with `Failed to find a free participant index for domain 0`, and goals, probes and `send_tree` never started. `bringup/config/cyclonedds.xml` (eigen_vector) now sets `<Discovery><MaxAutoParticipantIndex>120</MaxAutoParticipantIndex></Discovery>`; a process reads it when it starts, so no restart is needed. To list the participants: `ss -ulpn | grep -E ':74[0-9][0-9] '` gives one pair of ports per process; a process whose parent is `init` is a leftover (on 2026-11-04 an orphaned `ros2 topic echo /orchestrator/trace`).
2. **Orin** (camera, `seed_bbox`, `track_3d`): `scripts/deploy.sh launch orin`. Check that `/camera/camera/color/image_raw` publishes (from the NUC: `source ~/orchestrator_ws/env.sh && ros2 run orchestrator agent_cli topics`).
   - **Ready** when `~/eigen_vector_ws/launch.log` on the Orin has `RealSense Node Is Up!`, `Track3D node active!` and `Camera info received` (about 15 s; the TensorRT engines are already built in `~/eigen_vector_ws/models/edgetam-trt`). The camera runs at 6 Hz: `ros2 topic hz /camera/camera/color/image_raw`.
   - **Without deploy.sh** (when the laptop cannot reach the Orin through the NUC): `ssh -J vector@vector-nuc unitree@192.168.123.164`, then `tmux -L eigen_vector new-session -d -s eigen_vector "bash -lc 'source ~/eigen_vector_ws/env.sh && LD_PRELOAD=\"\$EIGEN_VECTOR_PRELOAD\" ros2 launch bringup orin.launch.py 2>&1 | tee ~/eigen_vector_ws/launch.log'"`. The Thor can also serve as the jump host (`-J vector@vector-thor`), since it is on the robot wire.
   - **Camera lost:** `xioctl(VIDIOC_QBUF) failed ... No such device` in the Orin log, and `lsusb | grep -i realsense` empty, means the RealSense dropped off USB (it did once when a cable was plugged in). Replug it, check `lsusb` and `ls /dev/video*`, then restart the Orin stack (stop it with Ctrl-C to the tmux pane: `tmux -L eigen_vector send-keys -t eigen_vector C-c`).
   - **After a reboot, check the clocks and the log's start.** The Orin's `launch.log` can still hold the last run's ready lines for a moment after the new launch starts. Compare `date +%s.%N` on the NUC and the Orin (they agreed within 0.3 s, NTP synced) and look for the ready lines after the new `[INFO] [launch]` header.
   - **Is it tracking the right thing?** Seed a button and look: `ros2 topic pub --once /keyboard_input std_msgs/msg/String "{data: down button}"`, then `agent_cli image overlay /tmp/o.jpg` and view the image; `ros2 run tf2_ros tf2_echo foot_world selected_target` gives the target in `foot_world`.
3. **Thor** (the Cosmos daemon and `ground_server`, in `~/eigen_vector_ws_bringup`): `scripts/deploy.sh launch thor`.
   - **The Thor can reboot on its own** (2026-11-04, 20:4x). It comes back with no stack, tailscale logged out (no internet) and its clock reset to 1970. Symptom: `seed_bbox` on the Orin logs `no grounding server on /vlm_ground_server/ground_boxes` and every reach check gives `NO_TRACK`. Reach it over the robot wire through the NUC: `ssh -o HostKeyAlias=vector-thor -J vector@vector-nuc vector@192.168.123.1`, then start the stack as `deploy.sh` does: `tmux -L eigen_vector_bringup new-session -d -s eigen_vector_bringup "bash -lc 'source ~/eigen_vector_ws_bringup/env.sh && ros2 launch bringup thor.launch.py 2>&1 | tee ~/eigen_vector_ws_bringup/launch.log'"`. It is up when the log has a new `grounding service on /vlm_ground_server/ground_boxes` and `docker ps` shows `edgellm` up. The 1970 clock does not affect the grounding service.
4. **Thor VLM endpoint** for `button_lit` and the other VLM checks. On the Thor, the first time: `git clone -b tingxual/vec-125-scope-the-agentic-reasoning-topic git@github.com:EigenVectorRobotics/orchestrator.git ~/orchestrator_ws/src/orchestrator` (later: `git -C ~/orchestrator_ws/src/orchestrator pull`). Then, in the vlm environment and a clean shell:

   ```bash
   tmux new -d -s cosmos_vlm "env -i HOME=$HOME USER=$USER LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin bash --noprofile --norc -c 'source ~/eigen_vector_ws_bringup/env.sh && exec python3 ~/orchestrator_ws/src/orchestrator/orchestrator/orchestrator/cosmos_vlm_server.py --port 8090'"
   ```

   Check from the NUC: `curl -s http://vector-thor:8090/v1/models` answers. It shares the Cosmos daemon with `ground_server`, one request at a time.
   `deploy.sh launch` reaches the Orin at `192.168.123.164` through the NUC, so the laptop needs that address's current host key in `known_hosts` (on 2026-11-04 the old entry was stale; the new key matched `unitree-g1-nx`'s).
5. **Executor** on the NUC, with the team's places file:

   ```bash
   tmux new -d -s orchestrator "bash -c 'source ~/orchestrator_ws/env.sh && exec ros2 run orchestrator executor_node --ros-args --params-file ~/orchestrator_ws/src/orchestrator/orchestrator/config/orchestrator.yaml -p places_file:=/home/vector/maps/two_floor_clean_cabin2.places.yaml'"
   ```

   Check: `tmux capture-pane -pt orchestrator` shows the skill count (32 since the agent trial: `press_judge`, `set_press_offset` and `clear_terrain_map` added) and "3 places". A `ground: No module named 'vlm_interfaces'` warning is expected.
   - **After editing the orchestrator,** rebuild (`cd ~/orchestrator_ws && source env.sh && colcon build --packages-select skill_registry orchestrator`; not a symlink install), then restart this session: `tmux kill-session -t orchestrator` and the command above. Contracts and Python load at start. A tree file is read at each `send_tree`, so a tree edit needs only the rebuild of `skill_registry` (its install copy), no restart. Check a tree first: `ros2 run skill_registry validate_tree <tree.xml>` (0 errors). Run the tests from each package's own directory (`cd ~/orchestrator_ws/src/orchestrator/orchestrator && python3 -m pytest -q test`, the same in `skill_registry`): from the repository root, `orchestrator` resolves to the installed package, and the tests check the old code. The places become `{freight_elevator_call_panel}`, `{inside_freight_elevator_button_panel}` and `{freight_elevator_call_panel_preapproach}`.
6. **Agent:** the session runs `agent_cli wait` in the background (step "Before the run" in the skill document). Another session's `agent_cli wait` also sees each call (`pgrep -fa agent_cli`); only one should answer. The session answers two skills: `press_judge` after every press (`skill_registry/agent_skills/press_judge.md`) and `manipulation_recovery` after a failure. In the agent trial the operator first asked to see each decision, then let the session send them all.
7. **Tree**, on the operator's go. The whole task is `freight_elevator_down_floor1_agentic.xml`: preapproach, call panel, EKF hold, down button, `press_judge`, door, cabin, EKF hold, floor 1, `press_judge`, with `manipulation_recovery` after each failure and the standard recoveries (README, "Standard recoveries"). To pick it up partway, in `orchestrator/test_trees/`:

   | Tree | Starts with the robot | Does |
   |---|---|---|
   | `freight_elevator_down_floor1_agentic.xml` | anywhere on the floor, localized | the whole task |
   | `freight_elevator_floor1_from_door_agentic.xml` | at the call panel, down pressed | waits for the door, walks in, floor 1 |
   | `freight_elevator_floor1_press_agentic.xml` | at the inside place | EKF hold, floor 1 |
   | `freight_elevator_floor1_walk_in_again_agentic.xml` | in the cabin, off the inside place by less than a reposition can fix | backs up to a pose 0.35 m behind the inside place, walks in, floor 1 |
   | `freight_call_elevator_down_agentic.xml`, `freight_call_elevator_agentic.xml` | anywhere | the older down / up press alone, checked by `press_reached` |
   | `freight_elevator_ride_agentic.xml` (**disabled for now**, not in use: the ride is `skill_registry/trees/freight_elevator_ride.xml`; untested) | outside the cabin on its start floor, door open | walks in, presses the target floor, waits for the IMU floor counter to read it, checks the door (`door_open`, `door_state_node`), walks out to `{exit_goal}`. Set `current_floor`, `target_floor` and `target_button` at its top. Needs `elevator_states`' counter on the NUC (`ros2 launch elevator_states imu_floor_detect_node.launch.py current_floor:=<the true floor> imu_topic:=/imu/data`; check `invert_z` and `accel_threshold` against the Mid-360's units first) and one place not yet saved: `exit goal` (see the tree's header) |

   Start it with `~/orchestrator_ws/src/orchestrator/scripts/start_cycle.sh [TREE.xml]` (default: the whole task). It starts in the background, each writing to `~/orch_runs/run_<HHMMSS>/`, the trace echo, `agent_cli wait` and `send_tree`, and prints the folder. The trees reset the body EKF themselves (`ekf_hold`: `reset_body_ekf`, then `body_ekf_calibrated`); the EKF-reset watcher and `~/ekf_reset.sh` are no longer used. When the tree ends, `scripts/stop_cycle.sh <run folder>` stops the helpers: an orphaned echo holds a DDS participant, and a `tail -F` left by a monitor outlives its shell.
   By hand, the same without the watcher:

   ```bash
   R=~/orch_runs/run_$(date +%H%M%S); mkdir -p $R; source ~/orchestrator_ws/env.sh
   nohup ros2 topic echo /orchestrator/trace --field data > $R/trace.txt 2>&1 < /dev/null &
   nohup ros2 run orchestrator agent_cli wait > $R/agent_wait.txt 2>&1 < /dev/null &
   nohup ros2 run orchestrator send_tree ~/orchestrator_ws/src/orchestrator/orchestrator/test_trees/freight_call_elevator_down_agentic.xml > $R/send_tree.txt 2>&1 < /dev/null &
   ```

   Read progress with `grep -o '"text": "[^"]*"' $R/trace.txt`, and poll for the event you wait for (`press_judge running`, `manipulation_recovery running`, `waiting for the session`, `<tree>: success|failure`). Kill the echo and the wait when the tree ends: an orphaned echo holds a DDS participant.
   - **Judging a press in a session.** `agent_cli show` prints the `press:` line (deepest point, sideways distance, `residual_m`) and the frame paths; `scp` the `press` and `release` frames (and crops) to the laptop and look at them. Floor buttons: lit ring in the press or release frame means pressed; finger on the face and no ring means not pressed. Call panel (no light): judge where the finger sits; the release frame, with the finger backing off, often shows it best. No frames (no pre-grasp seen): grab a live frame (`agent_cli image camera`) and use the numbers (E-018, E-019).
   - **A restart of the executor ends the run.** Cancel the tree first, and never restart with a call open. Contract and Python changes need the restart; tree changes only the `skill_registry` rebuild.

- **The down tree prepares the arm before each press** (subtree `prepare_arm`): Idle first when in HighLevel, then `set_mode high_level` and `set_profile visual_servo`. Arm commands stay blocked after `release_arms` until the next mode change ("Arm commands blocked: arms are released until the next mode change"), and a transition to the current mode fails; without the Idle step, a press after a release reports `Controller reached target` while the arm never moves. Then `no_stale_publishers`, `reset_filters`, `press_button`, and `press_reached` (the call panel does not light, so `button_lit` cannot judge it). After the press attempts it runs `release_arms` and `set_mode idle`; a reposition in `recover_press` releases the arms before it walks.
- **The down tree sets the walking state itself,** as `nav_outside.sh` does: `set_mode high_level` (`/unitree_interface/change_mode`; skipped when `in_high_level`, since a transition to the current mode fails), `clear_stop` (`/stop` 0) and `set_autonomy true` (`/autonomy_mode`), then the preapproach, a 3 s hold and the call panel, then `set_autonomy false` before the press. The press runs in HighLevel with the VisualServo profile, as in `down_once_right.sh`. The up tree does not do this yet: put the robot in that state first, or the walk routes and never moves (`navigate_to` times out at 120 s with the robot still). Never use `ready_locomotion` on a standing robot: it damps the robot, then stands it up again.
- **Goals carry z.** `two_floor_clean` has two storeys, and the route planner reads a goal with z 0 as the lowest storey. The orchestrator takes z from the places file (`pose_of: base_link`). A goal without z at the call panel snapped to z -7.4, unreachable, and `navigate_to` failed with `state = blocked for 10 s` at once.
- **The orchestrator on the NUC** must have the places loader (`places.py`) and the trees above, or the executor cannot read the team's places file. On 2026-11-04 they were copied from the laptop, uncommitted.

- **Button names:** `press_button`'s `target_description` goes to `GroundBoxes`, which accepts only `up button`, `down button`, `floor 1`, `floor 2`, `floor 3` (and `up`, `down`, `floor_1` ...) or `object:<description>`. Text such as "elevator up call button" is refused and the seed fails.
- **`release_arms` takes about 5 to 6 s** (5.7 s from the CLI in the agent trial). Its contract timeout was 5 s, and the tree that had just lit floor 1 failed on it; it is 12 s now.
- **Stop:** cancel the tree (`ros2 topic pub --once /orchestrator/cancel std_msgs/msg/Empty`), stop the robot as in "What we learned", release the arms (`ros2 service call /unitree_interface/release_arms std_srvs/srv/Trigger "{}"`) and go to Idle (`ros2 service call /unitree_interface/change_mode unitree_interface_msgs/srv/ChangeControlMode "{requested_mode: 1}"`); the next tree clears `/stop` itself, then `tmux kill-session -t orchestrator` on the NUC and `tmux kill-session -t cosmos_vlm` on the Thor, then Ctrl-C each `deploy.sh launch`.

## One press, step by step

1. **Walk.** Send the nav goal. Navigation needs `/stop` at 0, `/autonomy_mode` true and HighLevel mode. Turn autonomy off and return to Idle after arrival.
2. **Check reach before pressing.** Run `prepress.py <target>`. It seeds the button and prints its position in the pelvis frame.
   - **Look at a frame first.** The requested button must be in view. When floor 1 sat below the frame edge, Cosmos grounded floor 2 instead, and the press went to the wrong button.
   - **`NO_TRACK` is often random.** If the button is in frame, rerun the check up to twice before moving the robot.
   - **`NO_TRACK` with a large, close button** means the robot is too close. The D435 gives no depth under about 0.28 m.
   - **Ranges differ by panel.** Outside call panel: 0.33 to 0.48 m ahead (the operator called 0.338 and 0.354 m good). Inside floor panel: about 0.38 to 0.46 m; 0.52 to 0.53 m was too far, and at 0.38 m floor 1 can fall out of frame. Stay within 0.15 m sideways.
   - **Arm choice.** Outside, pick by side (left when y > 0). Inside, use the right arm for floor buttons: the left arm failed IK ("Planning failed: empty trajectory") at y +0.05 to +0.07 m and z 0.22 to 0.24 m above the pelvis. If planning returns an empty trajectory, try the other arm before moving the robot.
3. **Fix the position.** `correct.sh <dx> <dy>` switches to HighLevel, publishes a 0.2 m/s `TwistStamped` on `/local_movement_cmd_vel` for distance / speed seconds, sends zero, and returns to Idle. It is open loop. The mux passes it to `/cmd_vel` and the loco `Move`.
   - **Small moves are unreliable on legs.** An 8.6 cm back move went about 21 cm. Forward moves covered only about half the command (9 cm moved 2 cm, 21 cm moved 10 cm, 20 cm moved about 8 cm).
   - **Too far by 5 cm or more:** command forward about twice the error.
   - **Too close, or slightly too far:** step back 20 cm, then forward. Size the forward leg for the half-coverage, or the robot ends further away than before.
   - **After three failed corrections,** walk out and in again. Resending the same nav goal from where the robot stands can overshoot, and route_planner ignores goals closer than its tolerance.
   - **The walk overshoots the call-panel place by 3 to 6 cm.** `path_follower` `slow_down_distance_threshold` is set live to 0.7 (config default 0.4); a relaunch resets it.
4. **Reset the body EKF.** Call `/body_ekf/reset` once the position is good. The EKF assumes a still robot, and after a walk its yaw drifts up to 0.35 deg/s. That drift swings `foot_world`, which moves the tracked target by centimetres during a press.
5. **Press.** Run `attempt.sh <target> <tag>` with `ARM=left|right`. It bags the press (joints, commands, `/traj`, end-effector and track poses, RGB, aligned depth, lidar), grabs frames at 2 Hz, and prints the residual.
6. **Judge the press** with the three checks below, in order.

## Judge each press, in this order

1. **Did the robot move?** Run `moved.py`. A leg joint swing over 5 deg means a step, and the press is invalid. Retry it with no offset. A step happened only once, at 26.6 deg on the left knee.
2. **Was the tracking right?** Run `trackcheck.py`. Distrust the press when the target wanders more than about 20 mm in `foot_world`, or when torso yaw moves more than about 3 deg. Compare with the camera-frame track: a steady camera track with a moving `foot_world` target points at the EKF, not the tracker.
3. **Where did the fingertip end up?** Run `resid.py`. It prints fingertip minus tracked target in the target frame (x along the button normal, y sideways, z up).
   - Look at the hold frame too. The call-panel indicator does not light, so the image only shows where the gripper sits. The floor buttons light a ring when pressed.
   - Only after checks 1 and 2 pass, set the offset to the negative residual: `ros2 param set /move_to_pose_server arm_safety_distance_{x,y,z}`.
   - **How the offset works:** `to_pelvis_frame_()` in `move_to_pose/planning.cpp` shifts every target by (x, y, z) in the target's own frame (x along the normal, positive deeper). It reads the params at every plan, so a change applies to the next press with no restart. The final-leg stepping freezes the shifted goal in the left-ankle frame, and step 4 lost about 3 mm of a 5 mm depth offset there. The params live only in the running node and reset to 0 on relaunch.
   - **If an offset does not show in the residual,** compare the planned grasp with the reached depth (`depthcheck.py`). A planned depth the fingertip never reaches, with a waist yaw error jump at that moment, means contact, not a missing offset.

## What we learned

- **Most misses were not PID.** A free-air replay of the same arm commands tracked within 0.2 deg. The large errors came from steps, a drifting EKF, the hand meeting the panel, or bad positioning.
- **Repeated zero `/cmd_vel` used to starve the arm.** Each zero twist made `unitree_interface` call a blocking loco `Move`. A timed-out call froze arm commands for 10 s. path_follower now sends one zero and goes quiet (branch `fix/path-follower-idle`, not yet committed).
- **Offsets from a bad press make things worse.** Offsets taken from a press where the robot stepped, or the track drifted, sent the hand further off.
- **The successful floor 1 press** used the right arm at 0.426 m reach, with offset (+6.3, +7.8, +2.9) mm from the previous rim hit.
- **The inside right-arm residual repeats.** Three clean no-offset presses agree within 2 mm, averaging (-5.1, -7.8, -3.7) mm. With offset (+5.0, +7.5, +3.4) mm the sideways and height errors went to +1.6 and +1.2 mm, and the finger sat on the button face.
- **The open problem is pushing force.** With aim fixed, the finger still stops 4 to 7 mm short as waist yaw gives way about 0.5 deg (VisualServo waist kp 200, Ki 60). Self-collision is ruled out in the URDF. Options: a larger depth offset (about +15 mm), or a stiffer or locked waist yaw during the press.
- **body_ekf runs outside the NUC launch** after a reset, in `tmux -L body_ekf`. Stop it before a full relaunch, or two `body_ekf` nodes will run.
- **Stop the robot first, then clean up.** To stop, publish `/stop` 1 and `/autonomy_mode` false in their own command. Never chain them after a `pkill -f` whose pattern also matches your own shell, because that kills the stop command too.
- **Check localization after a bad walk.** The robot once walked off course and hit things because localization had slipped. Confirm `localized` and the inlier ratio before sending the next goal.
- **A walk that routes but never moves** has three causes seen on 2026-11-04. (1) New processes could not join DDS (see the participant limit in Bring-up): the goal never started. (2) `local_planner` found no free path: it publishes only one-pose stop paths on `/path` (count the poses), and `path_follower` sends nothing. Look at `/terrain_map` around the robot (intensity is the height above ground; above 0.1 m blocks): people, a stand or the gantry within 0.3 m of the robot blocked every path. `/clear_terrain_map` (`std_msgs/Empty`) clears latched points; points that come back are real. (3) The robot was not in HighLevel, or `/stop` was 1, or autonomy was off. `route_planner` logs a carrot only when its state changes, so no log line does not mean no routing: read `/route_planner/status` and `/nav_state` instead.
- **Watch navigation in Foxglove** in the `map` frame: the prior map is `/route_planner/terrain` (latched), the live obstacles `/terrain_map` and `/terrain_map_ext` (colour by intensity), the candidate paths `/free_paths` (empty means all blocked), the live scan `/registered_scan`. The places file is not published by the stack: to see it, publish a latched (`TRANSIENT_LOCAL`) `visualization_msgs/MarkerArray` in `map`, one arrow per place at x, y, z with yaw from `yaw_deg`, read from `~/maps/two_floor_clean.places.yaml` (`~/places_markers.py` on the NUC does this, in `tmux -L places`; it stops with a NUC reboot). `/path` poses carry `base_link` since the local_planner fix (uncommitted, navigation_stack).
- **Localization without an initial pose** assumes the robot starts at the map origin and searches yaw within +-30 deg. A match at the edge of that window (e.g. -33.7 deg) is suspect: check that `/registered_scan` lies on `/route_planner/terrain`, and send `/initialpose` from Foxglove if not.
- **foot_world drifts after a walk.** On 2026-11-04 the robot stood still after a press, and the tracked down button moved 12 cm sideways and 8 cm down in `foot_world` while the camera tracked the same button: the body EKF drifted, and the press went way off though the arm reached its goal. Reset the EKF at the panel right before the press, with the button in view. Since manipulation_stack `e63f2b8` (2026-11-08) body_ekf has a `~/reset` service: `ros2 service call /body_ekf/reset std_srvs/srv/Trigger "{}"`; it stops publishing `/body_ekf/torso_pose` until it has seen 2 s of stillness, then recalibrates. The trees do this in `ekf_hold` (`reset_body_ekf`, `body_ekf_calibrated`). Before that commit, the node was restarted instead (`~/ekf_reset.sh.restart_bak` on the NUC). Reset any earlier, and the EKF drifts again.
- **Press tests of 2026-11-08** (trees in `~/orch_runs/press_down_145709/` on the NUC, runs in the sessions `20261108T185515Z` to `20261108T233309Z`). The button poses came from the Thor: its `ground_server` answered grounding requests from 14:57 to 19:45 (`down`, then `floor_1/2/3`), and from session `20261108T202345Z` on the bags hold `/track_3d/selected_target_3d` from the Orin's track_3d. Only runs 007 to 010 of `20261108T185515Z` have no `selected_target_3d` and may have used `fake_button_pose.py`. The Thor's VLM endpoint (Cosmos, port 8090) was down that day, so `ask_vlm`-based checks such as `button_lit` were not available; `press_judge` (a Claude session through `agent_cli`) does not depend on it. The trees had no stance check and judged each press with `press_reached` (the arm against its goal, not whether the button lit). A press with no track at all ended in `press_button`'s 60 s timeout:
  - **Waiting longer after a walk does not help; recalibrating the EKF does.** A 35 s settle at the call panel left the press 28.8 mm off sideways, as the walks without it (28.3, 29.8 mm). Once the EKF reset ran through `/body_ekf/reset` with a 5 s hold, the inside presses mostly kept within 8 mm sideways.
  - **One inside stance serves every floor button.** From the inside place, floors 1, 2 and 3 in a row (`floors_cycle`, `tf_ab_*`, about 25 presses) with the offset (0.005, 0.0075, 0.0034): depth almost always within -12 mm; the misses were 8.1 to 11 mm sideways, just over the 8 mm limit.
- **Hand jumps come from a second publisher.** `no_stale_publishers` checks `/position_control`, `/feedback`, the arm target and `/reset_filter` (not counting the executor's own); a leftover replay or test script makes the hand jump. The trace names every publisher of a failing topic.
- **Status speech helps the operator.** Publish a short `std_msgs/String` on `/speak` with your diagnosis whenever a step fails or a press is skipped.
- **Arrivals are worse than 3 to 6 cm with the slow-down threshold at its default.** After a relaunch (`slow_down_distance_threshold` 0.4), the walks in the agent trial stopped up to 17 cm past the call-panel place and 24 cm past the inside place, each 8 cm or more past putting the button out of view (E-001, E-015). Set 0.7 live again after a relaunch, and look at the camera before a press.

## Agent trial, 2026-11-04

The whole task run by the orchestrator, 23:22 to 00:16 the next day, with the Claude Code session answering `press_judge` and `manipulation_recovery` (nine runs, `~/orch_runs/run_232207` to `run_001046` on the NUC; the earlier runs there are from before the trial). The task finished: down pressed and judged from its frame, the door seen open, the walk into the cabin, floor 1 lit. On the way it showed these, each with its fix (experience E-014 to E-020):

- **The old press check was wrong, the press was right.** `press_reached` reported 229 mm sideways after a press the operator saw work; the phase tracker had taken the arm's swing up for the push. Fixed in `press_capture.py`; the check is now `press_judge`, the session looking at the frame at the fingertip's closest point (E-014, E-019).
- **The robot stopped too close** at both panels (8 to 24 cm past the place), the button fell below the frame, Cosmos found 0 boxes, and `press_button` timed out at 60 s with no seed. Fix: reposition back (E-015).
- **Short repositions did not move the robot**: goals 0.148 and 0.18 m away ended at once. Repositions of 0.27 and 0.29 m walked. Make a reposition at least 0.25 m from where the robot stands; when that cannot reach the stance, walk out and in again (E-016).
- **Too far is out of reach without an error**: from 13 cm behind the inside place, `press_button` reported reached while the fingertip stopped 67 mm short (E-018).
- **The walk into the cabin found no free path** (latched terrain points 0.2 m ahead); one `/clear_terrain_map` freed it. Every walk now clears and retries (E-017).
- **Floor 1 needs depth.** On the face but unlit at offset +5 mm deep; lit at once at +12 mm (E-011, E-012, E-020, `offset_retry`).
- **Bugs found and fixed in the orchestrator** (all tested): the session's request lost a long failure (cut at 40 kB, `failure: none recorded` after a 60 s press); `set_press_offset` failed on its second call in a run (the contract's `fixed` list was turned into messages in place); a failed `press_judge` crashed the trace (`KeyError('elapsed_s')`); `release_arms` timed out at 5 s.
- **Bring-up**: the operator restarted the devices once, mid-trial; the NUC and the Orin came back with no stack, while the Thor had kept its stack and `cosmos_vlm` running. Localization matched on its own after both launches, without `/initialpose` (checked with `scan_fit.py`).

## Suggestions for lower latency and earlier action

- **Seed while walking.** Send the button query in the last metre of the walk, so the track is live on arrival. The seed alone takes 1.7 to 9 s.
- **Reset the EKF in parallel.** Call `/body_ekf/reset` as soon as the reach check passes. Calibration takes about 2 s.
- **Keep one ROS process alive.** Each `ros2` CLI call spends 1 to 3 s on discovery. A single Python node that walks, checks, sets params and presses would cut that time.
- **Decode on the NUC, not the laptop.** Extract only the small npz per press. Skip the camera topics in the npz.
- **Predict the position problem from the arrival pose.** Arrivals more than about 8 cm from the place pose usually fail the reach check, so correct straight away.
- **Check the EKF yaw rate before every press,** even after a reset. A rate above 0.05 deg/s means the reset did not take.
- **Seed the next offset from history.** Keep a per-panel, per-arm table of clean residuals. Apply its median before the first press, and update it after each clean press.

## Residual log (clean presses only, no offset unless noted)

Fingertip minus tracked target in the target frame: x along the normal (negative = short), y sideways, z up. "Clean" means no step and a steady track.

| Time | Panel | Button tracked | Arm | Reach (m) | x (mm) | y (mm) | z (mm) | Result | Bag |
|---|---|---|---|---|---|---|---|---|---|
| 18:25 | outside | up | left | 0.415 | -7.7 | -12.3 | -4.1 | not confirmed | press_debug_20261104_182519 |
| 18:33 | inside | floor 1 | right | 0.432 | -6.3 | -7.8 | -2.9 | finger on rim | press_debug_20261104_183313 |
| 18:36 | inside | floor 1 | right | 0.426 | -4.3 | -6.7 | -1.5 | pressed, offset (+6.3, +7.8, +2.9) | press_debug_20261104_183646 |
| 18:52 | outside | up | left | 0.413 | -8.7 | -0.2 | -5.1 | not confirmed | press_debug_20261104_185256 |
| 19:07 | inside | floor 2 (asked floor 1) | right | 0.391 | -4.3 | -8.6 | -4.9 | did not press | press_debug_20261104_190703 |
| 19:13 | inside | floor 2 | right | 0.379 | -4.6 | -6.9 | -3.2 | did not press, finger on the right rim | press_debug_20261104_191319 |
| 19:17 | inside | floor 2 | right | 0.380 | -6.5 | +1.6 | +1.2 | not lit; finger on the button face, offset (+5.0, +7.5, +3.4) | press_debug_20261104_191723 |

The 19:07 press asked for floor 1, but floor 1 was out of frame when Cosmos grounded, so it tracked floor 2. The reach check must confirm the requested button is in frame before pressing.

## Self-collision check, floor 2 presses (offline)

`selfcoll.py` runs Pinocchio and coal on the planner's URDF (`g1_29dof_with_hand_rev_1_0.urdf`, convex hulls, adjacent links skipped) at the measured and commanded hold poses. `depthcheck.py` compares planned depth, reached depth and joint error.

- No right-arm hull overlaps anything at the hold. The closest body pair is torso to right shoulder roll link, 19 to 23 mm; the hand and gripper are far from the body.
- The commanded hold pose is just as clear, so the planner did not trim the reach to avoid the body.
- The planner put the depth offset into the plan (+5.7 mm at step 3), but the fingertip stalled 4 to 7 mm short, with waist yaw error jumping to +0.5 deg at that moment.
- So the stall comes from the finger meeting the button, with waist yaw giving way. The URDF hulls rule out self-collision; parts missing from the URDF (cables, gripper mounts) could still touch.
