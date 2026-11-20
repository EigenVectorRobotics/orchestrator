# Troubleshooting

What to check and what to do when something goes wrong on the robot. Each row is a symptom, the checks in order, and the fix. How to start and stop things is in `docs/run.md`; what runs where is in `docs/machines.md`.

## Machines and processes

| Symptom | Check | Fix |
|---|---|---|
| A new process fails with `Failed to find a free participant index for domain 0`; a goal, a probe or `send_tree` never starts | CycloneDDS's automatic participant index range (32 by default) is full; orphaned processes hold an index too. `ss -ulpn \| grep -E ':74[0-9][0-9] '`: one pair of ports per process; a process whose parent is `init` is a leftover | Kill the leftovers (often an orphaned `ros2 topic echo`). On the NUC, `bringup/config/cyclonedds.xml` must set `MaxAutoParticipantIndex` 120 (`docs/machines.md`) |
| Preflight stops with "a robot control node runs twice", or warns that nodes run outside the launch | `python3 -m orchestrator.health.stale_nodes report` names each copy with host, pid and workspace | Stop the extra copy (e.g. a `unitree_interface` from `~/play_wav_ws`). A node restarted by hand in tmux still works, but stop it before a stack restart, which would start a second copy |
| `/feedback` is silent, or the NUC cannot ping the robot (`192.168.123.161`) or the Orin (`192.168.123.164`) | `cat /sys/class/net/enp86s0/carrier` is 1 with a link; `ip -br a show enp86s0` shows `192.168.123.5` | Reseat the cable. The NUC reboots with the robot when the battery is changed; then every machine's stack has to be started again |
| A ready line found in a `launch.log` belongs to the previous run | the log's first line, the `[INFO] [launch]` header of the new launch | Look for ready lines after the new header only; `tee` truncates the log a moment after the launch starts |
| The Orin log has `xioctl(VIDIOC_QBUF) failed ... No such device`; `lsusb \| grep -i realsense` is empty | `lsusb`, `ls /dev/video*` | The RealSense dropped off USB: replug it, then restart the Orin stack (`tmux -L eigen_vector send-keys -t eigen_vector C-c` on the Orin, then start it again) |
| Frames and poses do not line up in time | `date +%s.%N` on the NUC and the Orin (they agree within about 0.3 s when NTP is synced) | Fix the clocks before judging presses: frame times are the camera's stamps, pose times the NUC's |
| `seed_bbox` on the Orin logs `no grounding server on /vlm_ground_server/ground_boxes`; `button_in_view` finds no box and `check_reach` finds no target pose | Is the Thor up? It can reboot on its own and come back with no stack, tailscale logged out and its clock at 1970 | Reach it through the NUC (`ssh vector@192.168.123.1`) and start its stack (`docs/run.md`, section 6). The 1970 clock does not affect the grounding |
| `curl -s http://vector-thor:8090/v1/models` does not answer | `tmux ls` on the Thor: is there a `vlm_server` session? | Start `vlm_server` (`docs/run.md`, section 6). Only `ask_vlm` needs it; `thor_up` checks the grounding service only, so preflight goes on without it |
| The hand jumps during a press | `ros2 topic info -v <topic>` on the arm and target topics: more publishers than the stack's own? | Stop the leftover replay or test script that publishes there |

## Localization

| Symptom | Check | Fix |
|---|---|---|
| `/slam_elevatorlio/status` is not `"localized": true`, the first match was rejected (structure inlier ratio below 0.80), or a match landed near the edge of the yaw search window (about +-30 deg from the map origin) | Does `/registered_scan` lie on `/route_planner/terrain` in Foxglove? A right match has a median about 3 cm and 75 to 80 % of the scan within 10 cm, and the share falls when the scan is turned 5 or 10 deg | Send `/initialpose` from Foxglove (`ws://vector-nuc:8765`) when the check fails |
| The robot walked off course | `localized` and the inlier ratio | Check localization before the next goal; relocalize first |
| `slam_healthy` stays false; `/registered_scan` looks wrong; `door_state` says OPEN at a closed door | a LIO divergence in the SLAM log | Restart SLAM (the NUC stack); `/initialpose` does not clear it |
| The robot says "My localization failed. Stopping the task." during a task (the SLAM guard of `ride_to_goal`, outside the cabin); the task has ended | `localized` and `healthy` on `/slam_elevatorlio/status` | `localized` false: send `/initialpose`. `localized` true and `healthy` false: most likely a LIO divergence, so restart SLAM first, then relocalize. Then send the task again |
| Localization is lost after walking out of the cabin | was there a relocalization in place about 1 m out of the door? | `relocalize_here` there, standing still (`walk_out` does it: tracking alone rejects the larger correction a ride leaves) |

## Walking

| Symptom | Check | Fix |
|---|---|---|
| A walk routes but the robot never moves; `navigate_to` times out | In order: (1) the process started (DDS, above); (2) no free path: one-pose stop paths on `/path`, `/free_paths` empty, `path_follower` sends nothing, and `/terrain_map` has points close around the robot (intensity is the height above ground; above 0.1 m blocks); (3) HighLevel mode, `/stop` 0, `/autonomy_mode` true. `route_planner` logs a carrot only when its state changes: read `/route_planner/status` and `/nav_state` | (1) as above; (2) `/clear_terrain_map`: latched points go; points that come back after it are real (people, a stand, the gantry within about 0.3 m): move the obstacle or the robot; (3) `set_mode high_level`, `clear_stop`, `set_autonomy true`. A transition to the current mode fails, so skip `set_mode` when `in_high_level` |
| A second `navigate_to` to the place the robot already stands on times out at 120 s | the two goals in the trace | Give the preapproach a place of its own, or none: `floor_settings` (`skill_registry/trees/subtrees/elevator.xml`) sets `call_panel_preapproach` to `none` on floor 1, which has no preapproach place |
| The robot keeps turning in place instead of walking | `/slam_elevatorlio/status`: matches rejected for a large correction (`match rejected: correction ...` in `~/nav_launch.log`) | Stop it; send `/initialpose`; if matches stay rejected, restart the navigation launch |
| A walk is `blocked` from its first feedback, with `dist_to_goal` inf | the goal's z | The goal is on the wrong storey or off the route graph: give z (a place from the places file has it; a literal goal is `"x y z yaw"`) |
| The robot stops several centimetres off the place, or the button is out of view after the walk | the arrival on `/odom_base_link` against the place; `path_follower` `slow_down_distance_threshold` (config default 0.4) | Correct the stance before pressing; arrivals are an open problem (`docs/architecture.md`) |
| A reposition ends at once, without a step | the distance from where the robot stands to the goal, counting the sideways part | A reposition under 0.10 m from the robot walks 0.15 m behind its goal first, then onto it (the look does this) |
| The arm freezes for about 10 s after a walk | zero twists on `/cmd_vel` while the robot stands | `path_follower` must send one zero and then go quiet |

## Pressing

| Symptom | Check | Fix |
|---|---|---|
| A press after `release_arms` reports `Controller reached target`, but the arm never moved | the mode history: arm commands stay blocked after `release_arms` until the next mode change | Go to Idle, then HighLevel, then `set_profile visual_servo` before the press (subtree `prepare_arm`, in each look) |
| `press_button` times out at 60 s with `distance_to_goal` inf throughout | the camera: is the requested button in the frame and well above its bottom edge? `ground_server`'s log: `0 box(es)`? `/track_3d/selected_target_3d` publishing? | Button below the frame: step back. Button in frame but no track: seed again, up to twice. A large, close button with no track: the robot is under about 0.28 m, step back |
| The seed is refused | `press_button`'s `target_description` | Use a name the grounding accepts: `up button`, `down button`, `floor 1`, `floor 2`, `floor 3`. `ground_server` refuses `object:<description>` ("unsupported query"). Its call-button prompt calls the upper round button up and the lower one down: on floor 1, whose panel has one grey call button below a black round light, ask for `down button` |
| `press_button` succeeded, but the fingertip ended far short | the press's deepest point (the call's `press` in the run's `agent/<call id>/request.json`) | Out of reach: reposition closer |
| The finger lands beside, short of, or on the rim of the button, by millimetres | the recovery call's aim crops (`contact_aim`, `before_aim`) and `residual_m` | `offset_retry` (rules D2 and O1 to O4 in `g1_skills/g1_skills/manipulation_recovery/prompt.py`): with the aim crops, `oy` and `oz` come only from the miss of the fingertip from the face centre in `contact_aim`, and the residual gives only `ox`; when the face centre or the fingertip cannot be found there, `oy` and `oz` stay as in force; without crops, the residual-based offset. The model is told which part of the gripper is the fingertip and shown labelled references (`g1_skills/g1_skills/lib/fingertip.py`); `contact_aim` marks the estimated fingertip with a cyan crosshair. Not yet tested on the robot |
| The target wanders in `foot_world` while the camera-frame track is steady | the body EKF: was it reset after the walk? A yaw rate above 0.05 deg/s after the reset means it did not take | Reset it with the robot still, after the mode change and before `check_reach`: `look_here` runs `ekf_hold` once per stance, which calls `reset_ekf` |
| `reset_ekf` fails | `ros2 service list \| grep body_ekf` | `/body_ekf/reset` exists in body_ekf from manipulation_stack `e63f2b8` or later; it recalibrates after 2 s of stillness, and any motion restarts that |
| `release_arms` times out | how long the call takes (about 5 to 6 s) | Its contract timeout is 12 s; a shorter one fails |

## The orchestrator

| Symptom | Check | Fix |
|---|---|---|
| A tree is rejected | the trace's `validation` lines; `ros2 run skill_registry validate_tree <tree.xml>` | Fix the errors. A skill whose contract has `maturity: disabled` is refused; the executor also refuses trees while the recorder is not ready, and task trees until a preflight has passed since the launch ("no preflight has passed since the launch": send the task to the planner, or `send_tree --skip-preflight`) |
| Preflight fails and the planner does not plan | the trace: which subtree, and what it said | Fix that machine or check (sections above); then send the task again |
| An agent call goes unanswered | the trace's `agent_link` lines: is the executor connected to the agent bridge? The bridge window: is an `operator_confirm` question waiting? | Start the bridge on the laptop, or answer `y` or `n` in its window. With no word from the bridge for `UNREACHABLE_SAY_S` the robot says "I lost the connection to my helper. Waiting for it to come back."; after `UNREACHABLE_FAIL_S` the call fails ("My helper is still not connected. Going on without it."), and later calls fail at once until the bridge is heard from again, so the press slot ends by asking a person to press (both in `orchestrator/orchestrator/agent/link.py`). A connected bridge that does not answer fails the call at the skill's contract timeout (`timeout_s`) |
| Every agent call fails at once with "protocol mismatch" | the bridge window, or the call's `failed` event: the executor's and the bridge's `protocol.PROTOCOL` | The laptop and the NUC run different checkouts. Pull the NUC's branch on the laptop and restart the bridge (`mission_control/README.md`) |
