---
name: manipulation_recovery
description: press_button failed. Work out why from the evidence, decide whether the target was out of the arm's reach, and if so where the robot should stand instead.
runs_when: [press_button failed, the reposition walk arrived]
runs_on: a Claude Code session through agent_cli (default), or a model inside the executor on the NUC
decisions: [reposition, switch_arm, retry, ask_help, abort, continue]
---

# Manipulation recovery

The behavior tree calls you at two moments: after `press_button` has failed (the diagnosis), and after the walk to a new stance that you chose has arrived (the view check). Each time the tree waits, up to 600 s, for your decision. You cannot move the robot: you gather evidence with read-only tools, build whatever checks you need in Python, and end with one decision. The tree then carries out your decision with its own skills (`navigate_to`, `press_button`, `say`).

You are usually a Claude Code session that reaches the robot over ssh and answers with `agent_cli`: read "When you are a Claude Code session" first. You may also be a model inside the executor, with the tools in "Tools".

Your main question is: **was the target out of the arm's reach from where the robot stood?** If yes, where should the robot stand so that it is in reach? If no, what else went wrong?

This document gives you facts about the robot and its software, and where to find more. It does not give you a rule that answers the question. Measure, compute, and say in your reason what you found. What the team has seen in experiments is in a separate file; "Using the experience file" says how to use it.

## How to work

1. **Read the failure and the history.** `get_failure()` and `get_history()`. Note the message, the shape of the feedback over time, and what was already tried in this task. The failure is either a failed `press_button`, or a failed check: `skill: button_lit` with `checks: press_button`, where the press reported success and the button did not light; then `checked_call` is that press, with its feedback.
2. **Read the experience file** and note the entries whose symptom matches this failure. They are hypotheses to test in the steps below, not answers.
3. **Find where the target was.** Take the target pose from the failure record (`current_goal`, in `foot_world`). **It is already the upright goal, i.e. the press pose the arm was sent: use it as it is, do not flip or re-derive its orientation** (see "Target pose conventions"). If track_anything is still tracking, `/track_3d/selected_normal_pose_filtered/arm_upright` is a second source in the same convention; `/track_3d/selected_normal_pose` is the raw pose in the other convention. Transform it into `pelvis` with TF. The robot has not moved since the failure, so the current TF is valid.
4. **Check reach yourself.** Write a check in `run_python`: load the URDF and `g1.yaml` with pinocchio, apply the joint limits and the locked waist from the active profile, and solve IK for the press pose and the pre-grasp pose (both described below) for the arm that was used, then for the other arm. Use coal for self-collision if you need it. Look at the residuals and the joint margins, not only at a yes or no.
5. **If it is out of reach, search for a stance.** Shift the target by candidate base moves (the inverse of moving the robot) and solve again. The robot does not stop exactly where it is sent (experience E-001), so prefer a stance from which the target stays reachable across that spread, not one at the edge. **Look at the panel's angle too:** the angle between the goal's +X (the direction into the panel) and the pelvis +X, both in the horizontal plane. When it is large, e.g. above 45°, the robot is standing at a slant to the panel: include a turn (`dyaw`, toward the panel) in the stance search, not only a shift.
6. **Weigh the other explanations.** Perception (no seed, lost tracking, a wrong object), a timeout while the arm was still moving, or a transient rejection. Say why the evidence favors one. **A perception problem is `ask_help` for now**, not a retry: the team wants a person to look at it.
7. **Check that the button is still in view.** Look at the camera image, and project the target position into the camera with the camera intrinsics and TF. If the button is not in view from the current stance, or would leave the view from the stance you are about to choose, say so: a press cannot work on a button the camera cannot see.
8. **Call `decide` once.** If the evidence is missing or contradictory, say so and choose `ask_help`. Do not guess a reposition. If the button is out of view and no stance within reason brings it back, choose `abort`.

### After the reposition walk

When `navigate_to` to your new stance has arrived, the tree calls you again (phase `view_check`) before it presses. Your job now is short: **is the button still in view?**

1. Read where the robot actually stopped (`/odom_base_link`) and compare it with the goal; it can be off by about 0.1 m.
2. Look at the camera image (`get_image("camera")`) for the button you were asked to press.
3. Project the target position you computed in the diagnosis into the camera from the current pose, using TF and `/camera/camera/color/camera_info`, and check that it falls inside the image with depth in front of the camera.
4. Call `decide` with `continue` if the button is in view, or `abort` if it is not. When the image and the projection disagree, say so in your reason and trust the image.

## Using the experience file

`manipulation_recovery.experience.md`, in the same directory as this file, records what the team has seen: symptoms, causes, what helped, each with a status (confirmed n times, reported, suspected) and the conditions it was seen in. Read it with `read_file` (in a session, read it directly).

- **It is a prior, not proof.** Use a matching entry to choose what to check first; conclude only from this failure's evidence. An entry seen under other conditions (another panel, stance, software version) may not apply. Weigh confirmed entries above reported ones, and reported above suspected.
- **Cite what you used.** Name the entry ids in your reason, and say whether this failure fits them or contradicts them.
- **Do not edit it.** In a session, when this failure shows something new or an entry did not hold, draft the new entry or the change in the file's format and show it to the user with your decision; it goes in only after the user approves the text. Inside the executor, put that draft in your reason instead.

## When you are a Claude Code session

The executor publishes each call on `/orchestrator/agent/request` and waits for one decision. `agent_cli` (in the orchestrator package) is your only way to answer. Run it on the computer where the executor runs, over ssh, in the executor's ROS environment. [TODO: the workspace the orchestrator is deployed in on the robot; until then, the environment the team names for the run.]

**Before the run.** Start `agent_cli wait` in the background. It returns, with a summary of the call, when the tree opens one.

**Each call.** Do the steps in "How to work" with these commands in place of the tools:

| Tool | In a session |
|---|---|
| `get_failure`, `get_history` | `agent_cli show --full`: the failure with every feedback sample, and the history. `agent_cli show` alone gives the summary |
| `ros_read` | `agent_cli tf TARGET SOURCE`, `agent_cli topic TOPIC`, `agent_cli topics` |
| `get_image` | `agent_cli image camera OUT.jpg` or `agent_cli image overlay OUT.jpg`; the frames a press kept are files on the executor's computer, at the paths in the check's `outputs.frames`. Copy them to where you can view them |
| `run_python` | write the code to a file, then `agent_cli python FILE`: the same sandbox, with pinocchio and coal |
| `read_file` | read the files directly (`cat`, `sed -n`), read-only |
| `decide` | `agent_cli decide DECISION --reason "..." [--dx --dy --dyaw] [--left-arm or --right-arm]` shows what would be sent; add `--send` to send it |

**Rules for a session.** Nothing enforces these for you, unlike the tools inside the executor, so keep them yourself:

- `agent_cli decide --send` is the only thing you publish. Do not publish topics, call services, send action goals, start, stop or restart nodes, or change files, parameters or configs on any robot computer.
- **Get the user's go before you send.** Show the user the decision, the reason, and the evidence it rests on (the feedback shape, the target position in `pelvis`, what your check computed); run `--send` only after the user says go. Edits to the decision: show it again. [This becomes automatic when the team says so.]
- Watch the clock: `agent_cli show` says how long the call has been open. At 600 s the tree stops waiting and goes to its fallback, a request for help. Tell the user when time is short.
- After a `reposition`, the tree walks, then calls you again for the view check: run `agent_cli wait` again.
- If `--send` comes back `rejected`, read the message, fix the decision, and show it to the user again.

## Tools

When you run inside the executor (`orchestrator/agent.py`), these are your tools. None of them publishes or moves the robot.

| Tool | What it does |
|---|---|
| `get_failure()` | The last failed action call of this tree, as the tree recorded it: `skill`, `inputs` (for `press_button`: `target_description`, `left_arm`), `message` (the action's own result message, or the orchestrator's `timeout after 30 s` when it cancelled first), `outputs` (`final_distance`, `orientation_error_deg`), `elapsed_s`, and `feedback`: every feedback sample as `{t: seconds since the goal was sent, distance_to_goal, orientation_error_deg, tracking_lost, elapsed_time, current_goal}`, where `current_goal` is the target pose in `foot_world` |
| `get_history()` | Every finished action call of this tree (without feedback) and your earlier decisions with their reasons and offsets, oldest first |
| `read_file(path, offset, limit)` | Reads a text file or lists a directory, only under `/home/vector/eigen_vector_ws`, the agent skills directory and the scratch directory |
| `ros_read(op, ...)` | `op: tf` with `target_frame`, `source_frame`: the latest transform, i.e. the pose of `source_frame` in `target_frame` (translation, quaternion, roll/pitch/yaw, age). `op: topic_once` with `topic`: the next message (images and point clouds are refused; use `get_image`). `op: topic_list`: topics and types |
| `get_image(source)` | `camera`: the last color frame the executor has (`/camera/camera/color/image_raw/compressed`) [TODO: check that the Orin publishes this compressed topic]. `overlay`: the next frame of `/track_3d/tracked_overlay`, with the tracked mask and the tracker state text; it is published on every frame, also while nothing is tracked. `press_baseline`, `press_press`, `press_release`: the frames the last press kept for `button_lit` |
| `run_python(code)` | Runs Python 3.12 with `numpy`, `scipy`, `pinocchio` 4.1.0 and `coal`; returns stdout, stderr and the exit code. Pass data from the other tools in as literals. It runs in the scratch directory, which is also on `sys.path`: save helpers there and import them in later calls. Limits: 60 s, 4 GB, nice 19 |
| `decide(decision, reason, dx, dy, dyaw, use_left_arm)` | Ends your turn; see "Your decision" |

**What `run_python` blocks, and how far to trust it.** An audit hook stops imports of `rclpy` and other ROS client libraries, network connections, new processes, and writes outside the scratch directory; ROS discovery is off in its environment. The NUC allows no user namespaces, so there is no stronger sandbox: this guards against mistakes, not against code written to get around it. Do not try to get around it; use `ros_read` for ROS data.

You have at most 30 model turns for one call. In one task the tree allows a few diagnoses (`max_rounds`, 3 by default); once you choose `ask_help`, the tree does not call you again in that task.

## Where to find things on the NUC

The deployed workspace is `/home/vector/eigen_vector_ws` (`env.sh` there sets up the environment). The NUC holds other workspaces too; do not use them.

| What | Path |
|---|---|
| Robot model the arm planner uses | `install/g1_description/share/g1_description/assets/g1/g1_29dof_with_hand_rev_1_0.urdf`. Mesh paths in it are relative to that directory (`meshes/...`, collision meshes in `meshes/collision/`); pass the directory as the package directory when you load it |
| Model `robot_state_publisher` uses (has `camera_link`) | `g1_29dof_with_hand_rev_1_0_ros.urdf` in the same directory |
| IK profiles, joint limits, end effectors | `install/g1_pilot/share/g1_pilot/config/g1.yaml`; the active profile is named by `active_config` |
| Visual servo node | `src/manipulation_stack/humanoid_arm_skills/humanoid_arm_skills/humanoid_arm_visual_servoing.py` |
| Target filter, upright and approach poses | `src/manipulation_stack/humanoid_arm_skills/humanoid_arm_skills/pose_filtering.py`, `utils.py` |
| Arm planner and its IK | `src/manipulation_stack/g1_pilot/src/move_to_pose/`, `src/manipulation_stack/g1_pilot/src/core/arm_mp/` |
| Launch defaults on this robot | `src/bringup/launch/nuc.launch.py`, `src/manipulation_stack/humanoid_arm_skills/launch/humanoid_arm_visual_servoing.launch.py` |
| Navigation parameters for the G1 | `src/navigation_stack/local_planner/config/unitree_g1.yaml`, `src/navigation_stack/route_planner/config/unitree_g1.yaml` |

track_anything does not run on the NUC; it runs on the Orin with the RealSense camera, and its VLM runs on the Thor. You can read its topics, not its code.

## Live topics and TF

| Name | What it holds |
|---|---|
| `/track_3d/selected_normal_pose` | track_anything's target pose, in the color camera frame |
| `/track_3d/selected_normal_pose_filtered/arm_upright` | The filtered, upright goal the arm is sent, in `foot_world` |
| `/track_3d/is_tracking` | Whether the 2D tracker holds a mask (see "Perception facts") |
| `/track_3d/tracked_overlay` | Camera image with the mask, box and tracker state text |
| `/left_ee_pose`, `/right_ee_pose` | End-effector poses, in `foot_world` |
| `/odom_base_link` | The robot's pose in `map`, used by navigation |
| `/route_planner/status` | JSON: `state` (`routing`, `arrived`, `blocked`), `dist_to_goal` |
| TF `pelvis`, `torso_link`, `foot_world` | Body frames; `foot_world` is published by `body_ekf` |
| TF `left_tcp`, `right_tcp` | The end-effector points: `*_wrist_yaw_link` plus 0.1847585 m along X (`nuc.launch.py`) |
| TF `camera_link` | Fixed on `torso_link` (from the `_ros` URDF). [TODO: check that the camera driver's color optical frame is a child of it] |
| TF `base_link` | Navigation base; `sensor` to `base_link` is a static −0.35 m in z (`nuc.launch.py`) |

**The target may be gone by the time you look.** The servo node resets track_anything (`/track_3d/reset`) when the goal is cancelled, times out, fails to seed, or succeeds. The orchestrator's 30 s timeout cancels it, so after a timeout the live topics usually hold nothing. After a motion failure (`Planning failed: ...`) there is no reset and the tracker may still publish. The failure record's `current_goal` is the reliable source.

## The pipeline that failed

`press_button` calls the action `humanoid_arm_visual_servoing`. One call runs these steps:

1. **Seed.** The servo node publishes `/track_3d/reset`, waits 0.3 s, then publishes the target text on `/keyboard_input`. track_anything asks its VLM for a box, then tracks it.
2. **Target pose.** track_anything publishes the normal pose in the camera frame. `arm_pose_filter` transforms it into `foot_world`, filters it (Kalman by default) and publishes the upright version, which the servo node uses as the goal.
3. **Plan.** The servo node sends one `/move_to_pose` goal. move_to_pose transforms it into `pelvis` and plans: IK for the pre-grasp pose, a joint-space path to it, then IK along a straight final leg to the press pose, then a retreat.
4. **Track and replan.** While the arm moves, move_to_pose replans against the live target when it moves by at least 0.01 m or 0.05 rad (at most every 0.25 s). When the final leg starts, the goal is frozen in `left_ankle_roll_link` and the final leg runs in 4 steps, each re-mapped into the current pelvis because the pelvis leans while the arm reaches out (the pelvis correction).
5. **Done.** move_to_pose reports `Target reached`; the servo action succeeds with `Controller reached target`.

## Target pose conventions

- **track_anything normal pose:** position is the centroid of the button's plane; local **+X points out of the surface, toward the camera**. Only X is meaningful; roll about X is arbitrary.
- **Upright goal:** the same position, with a yaw-only orientation whose **+X points into the surface**, horizontal (`upright_pose(..., flip=True)` in `utils.py`). The end effector must point horizontally into the panel. **This is what the arm is sent and what the feedback's `current_goal` holds** (the servo node takes it from `/track_3d/selected_normal_pose_filtered/arm_upright`). With the safety offsets at 0, it is the press pose itself.
- **Pre-grasp:** the upright goal backed off **0.10 m along its own local −X**, toward the robot (`approach_offset` default in `visual_servo_planner.h`). The arm reaches the pre-grasp first, then pushes 0.10 m along +X. From `current_goal`: pre-grasp = `current_goal` translated by (−0.10, 0, 0) in its own frame.
- **Safety offset:** `arm_safety_distance_{x,y,z}` is added in the goal's local frame after the transform to `pelvis`; all default to 0.0.

Both poses must be reachable: the press pose is the far one, and the pre-grasp is 0.10 m nearer the body, which can be too close to the torso when the panel is near.

## Arm model facts

Read the files for exact values; these are the points that matter most.

- **The waist is effectively locked.** The active profile `arms_with_waist` limits waist yaw, roll and pitch to ±0.0001 rad, so the torso cannot lean to reach further. Legs and grippers are locked too.
- **Shoulder joint limits are tighter than the URDF's.** `g1.yaml` overrides shoulder pitch, roll and yaw (`joint_limit_overrides`); use those, not the URDF limits.
- **End effector:** `offset: [0.1847585, 0, 0]` from `*_wrist_yaw_joint`, the same point as TF `*_tcp`.
- **`collision_margin: -0.01`** in the active profile.
- **The real planner uses a different solver from yours.** g1_pilot solves IK with CasADi in C++ (`Humanoid::solve_ik`); there is no Python binding. Your pinocchio check uses the same model and limits but another solver, so near the edge of the workspace the two can disagree. Say so in your reason when your result is marginal.

## What each failure looks like

The result message tells you which stage stopped, but often not why. Use the feedback curve and the target position to tell the causes apart.

| Message | Where it comes from | What it means |
|---|---|---|
| `Planning failed: empty trajectory` (or `no waypoints`) | move_to_pose initial plan | The planner returned no trajectory: IK for the pre-grasp failed, the joint-space search found no path, or IK failed partway along the final leg. The code does not say which |
| `Planning failed: <other text>` | move_to_pose | An exception from the planner; read the text |
| `Target pose transform failed` | move_to_pose | No TF from the goal frame to `pelvis` within 0.5 s |
| `No joint feedback received yet` | move_to_pose | The arm controller is not publishing joint state |
| `move_to_pose goal rejected` / `move_to_pose send failed: ...` | servo node | move_to_pose refused the goal or was unreachable |
| `No target seed after N request(s)` | servo node | track_anything never started tracking. Each request waits `seed_timeout` 30 s; `seed_max_requests` is 2 |
| `Trajectory cleared` | move_to_pose | Something cleared the trajectory (the cancel service) |
| `Preempted by a newer goal` / `Cancelled` / `Cancelled while planning` | move_to_pose | Another goal or a cancel ended it |
| `Action timed out` | servo node | `action_timeout` of 600 s |
| `goal rejected` (orchestrator) | servo node | Another servo goal was active, or the call came within `post_goal_cooldown` 1.0 s of the last success |
| (a failed check) `button_lit` after `press_button` succeeded | the VLM check after the press | The arm reported `Controller reached target`, but the VLM saw the button lit in neither the press frame nor the release frame. The check's `outputs` hold the answers per frame (`not_lit`, `unsure`), the saved frames (`frames`: baseline at the pre-grasp, press at the deepest push, release 0.5 s after the retreat began; `*_crop` are the crops around the button box that the VLM was shown) and the times and boxes (`capture`: `pregrasp_t`, `deepest_t`, `release_t`, `max_depth_m`, `boxes`). A frame with no box of its own was cropped with the baseline's box; no box at all means whole frames were asked, which the message says. The arm reached its goal, so the button was within reach of that goal: look at the frames (`get_image press_baseline`, `press_press`, `press_release`), at where the goal was (`checked_call`'s `current_goal`), and at `max_depth_m` (about 0 means the push reached the goal) before blaming reach. A missing frame (no `pregrasp_t`, or no `deepest_t`) means the phase was not seen |

**When the orchestrator times out first.** `press_button` has `timeout_s: 30`, so the orchestrator cancels the action at 30 s, the leaf message is `timeout after 30 s`, and the action's own message is lost. Several failures end this way:

- **Seed never arrived:** `distance_to_goal` stays `inf` the whole time, because there is no goal pose yet.
- **Replanning keeps failing:** a failed replan does not end the goal. move_to_pose logs `Replan failed: ...; holding position` and the arm holds. A failed final-leg step does the same. `distance_to_goal` stops falling and stays flat at a nonzero value while `tracking_lost` stays false.
- **Arm still moving:** `distance_to_goal` is still falling at the timeout. Joints move at most 0.5 rad/s (0.25 rad/s on the final leg), so a long reach can simply be slow.

**Feedback fields.** `distance_to_goal` is the distance between the end-effector point and the upright goal, both in `foot_world`; it is `inf` until both exist. `tracking_lost` becomes true when no filtered target has arrived for `lost_timeout` 5 s. Feedback comes at 2 Hz.

## Perception facts (track_anything, main)

- `/track_3d/is_tracking` true means the 2D tracker has a mask, **not** that a 3D pose was published on that frame. An object can be dropped later (too few depth points, outlier filter, failed box fit) with no message on the topic.
- When the tracker loses the object, the overlay shows `Lost (<seconds>s)` in red and `is_tracking` is false. After `tracking_timeout` 20 s it stops and does not re-query on its own.
- A seed can lock onto the wrong object (experience E-003). If the target position is implausible for the button you were asked to press, suspect perception before reach: walking closer will not fix a wrong target.
- The normal is recomputed from a plane fit every frame and is not smoothed by track_anything; the arm filter smooths it.

## Navigation facts

From the deployed `navigation_stack` (main @ `6242b3e`) and its G1 configs. How precisely the robot actually stops is in the experience file (E-001, E-002).

- **`navigate_to` ends on arrival.** It calls the route planner's action `/route_planner/navigate_to_goal`, which succeeds only when the local planner reports `/goal_reached` with the robot within 0.5 m of the goal. When it fails, the result's reason says why (e.g. cancelled, replaced by a new goal, autonomy off). The `arrived` state on `/route_planner/status` comes earlier, when the goal is within `lookahead` (2.0 m) along the route; it is not the arrival.
- **Arrival thresholds in the config:** `goal_reached_threshold` 0.15 m and `goal_yaw_threshold` 0.15 rad (`local_planner/config/unitree_g1.yaml`). The robot stops within them, not on the goal; the stance actually seen is in E-001. [TODO: the heading accuracy at the end of the approach.]
- **The robot's pose** is on `/odom_base_link` (frame `base_link`). Compare it with the goal to see where the robot actually stood when the press began.
- **The follower is holonomic.** Side and back steps are slower than forward ones (`max_lateral_speed` and `max_backward_speed` 0.15 m/s).

## Your decision

Call `decide(decision, dx, dy, dyaw, use_left_arm, reason)` exactly once.

After a failed press:

| Decision | When it fits | Fields |
|---|---|---|
| `reposition` | The target was out of reach, or at the edge of reach, from where the robot stood, and a different stance would put it in reach | `dx`, `dy` in metres and `dyaw` in radians: the change of the robot's stance relative to the **base goal** the tree gave you (the original place, e.g. `{call_panel}`), in that goal's frame (x forward along its yaw, y to the left). The tree applies it to the base goal and walks there. Offsets do not add up across rounds: a second reposition states the whole offset from the base goal again |
| `switch_arm` | The target is in reach of the other arm from the current stance | `use_left_arm` |
| `retry` | The cause was transient (rejection, cooldown, a slow but progressing reach) and the same call is likely to work | none |
| `ask_help` | Perception failed (no seed, lost tracking) or found the wrong object; the evidence is missing or contradictory; or the history shows repositioning has already failed | none |
| `abort` | The button is out of the camera's view and no stance you can justify brings it back | none |

After the reposition walk:

| Decision | When it fits | Fields |
|---|---|---|
| `continue` | The button is in view; the tree presses again | none |
| `abort` | The button is not in view from where the robot stopped | none |

**`abort` ends the task early:** the tree stops at once and fails, with your reason as the error message. Use it instead of a press that cannot work.

**Your reason** must say, in two to four sentences: the evidence you used (message, feedback shape, target position in `pelvis`), what you computed and with which solver, which cause you concluded, and why the change you chose should help given the navigation tolerance. For `reposition`, state where you expect the target to be in `pelvis` after the move. For `abort`, state what you saw (the image, the projection) and where the button was expected to be. The reason goes to the trace for the team to review.

Do not repeat a reposition that the history shows has already failed in the same direction; after two repositions that did not work, choose `ask_help`.

## Sources

The facts above were read from these versions; update this file when they change.

- manipulation_stack: GitHub `pelvis-correction` @ `d70b9d1`; the NUC runs `test/pelvis-correction` @ `0554dd4` (that commit plus a merge of main, which adds one line to `body_ekf.cpp`). Files: `humanoid_arm_skills/humanoid_arm_skills/{humanoid_arm_visual_servoing,pose_filtering,utils}.py`, `humanoid_arm_skills/launch/humanoid_arm_visual_servoing.launch.py`, `g1_pilot/src/move_to_pose/{server,planning,control}.cpp`, `g1_pilot/src/core/arm_mp/{visual_servo_planner,joint_space_planner}.cpp`, `g1_pilot/src/body_ekf.cpp`, `g1_pilot/config/g1.yaml`, `g1_description/assets/g1/*.urdf`
- track_anything `main` @ `f5a1f47` (`trt` is merged into it): `track_anything/track_3d.py`, `track_anything/pointcloud_utils.py`, `track_anything/transform_utils.py`, `track_anything/track_3d_parameters.yaml`, `launch/track_3d.launch.py`, `README.md`
- NUC `/home/vector/eigen_vector_ws`: `src/bringup/launch/nuc.launch.py`, `src/bringup/README.md`, `src/navigation_stack` main @ `6242b3e` (`route_planner/route_planner/{route_planner_node,planner}.py`, `route_planner/route_planner/route_planner_parameters.yaml`, `local_planner/src/local_planner.cpp`, `local_planner/src/local_planner_params.yaml`, `local_planner/config/unitree_g1.yaml`, `route_planner/config/unitree_g1.yaml`); Python packages under `/opt/ros/jazzy/lib/python3.12/site-packages`
- orchestrator: `skill_registry/contracts/{press_button,navigate_to}.yaml`
- Observations, with their own sources: `manipulation_recovery.experience.md`
