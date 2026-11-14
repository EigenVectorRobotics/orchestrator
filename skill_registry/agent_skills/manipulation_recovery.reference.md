# Manipulation recovery: reference

Facts that hold by construction: tools, paths, topics, the press pipeline and its failure messages. `manipulation_recovery.md` says how to use them. Observations live in `manipulation_recovery.experience.md`.

## Tools

None of these moves the robot. Only `say` publishes, and only speech.

| Tool | What it gives |
|---|---|
| `get_failure()` | The last failure: `skill`, `inputs`, `message` (or `timeout after 60 s` when the orchestrator cancelled first), `outputs` (`final_distance`, `orientation_error_deg` at the closest approach), `elapsed_s`, and every feedback sample `{t, distance_to_goal, orientation_error_deg, tracking_lost, current_goal}`. A failed check carries `checked_call`. A press's `residual_m` is in the call's `press` |
| `get_history()` | Every finished call of this tree, including each `set_press_offset`, and your earlier decisions, oldest first |
| `read_file(path, offset, limit)` | Only under `/home/vector/eigen_vector_ws`, the agent skills directory and the scratch directory |
| `ros_read(op: tf, target_frame, source_frame)` | Pose of `source_frame` in `target_frame`, with age |
| `ros_read(op: topic_once \| topic_list)` | The next message (no images or clouds), or the topic list |
| `get_image(source)` | `camera`: last color frame, 1280x720 at 6 fps. `overlay`: tracker mask and state. `press_baseline`, `press_press`, `press_release`: the last press's frames |
| `run_python(code)` | Python 3.12 with numpy, scipy, pinocchio 4.1.0 and coal. Runs in the scratch directory, which is on `sys.path`. Limits: 60 s, 4 GB |
| `say(text)` | One sentence on `/speak`; ≤ 200 characters, ≥ 3 s apart |
| `note(text, about, key, value, evidence)` | Adds an observation to the knowledge store |
| `decide(...)` | Ends the call. The executor checks it and replies accepted, or why not |

`run_python` blocks ROS client imports, network, new processes and writes outside scratch. It guards against mistakes only. Use `ros_read` for ROS data.

## Where things are on the NUC

Under `/home/vector/eigen_vector_ws`. Ignore other workspaces.

| What | Path |
|---|---|
| Arm planner's robot model | `install/g1_description/share/g1_description/assets/g1/g1_29dof_with_hand_rev_1_0.urdf`; pass its directory as the package directory for meshes |
| Model with `camera_link` | `g1_29dof_with_hand_rev_1_0_ros.urdf`, same directory |
| IK profiles, joint limits, end effectors | `install/g1_pilot/share/g1_pilot/config/g1.yaml`; `active_config` names the profile |
| Visual servo node | `src/manipulation_stack/humanoid_arm_skills/humanoid_arm_skills/humanoid_arm_visual_servoing.py` |
| Target filter, upright and approach poses | same package: `pose_filtering.py`, `utils.py` |
| Arm planner and IK | `src/manipulation_stack/g1_pilot/src/move_to_pose/`, `src/manipulation_stack/g1_pilot/src/core/arm_mp/` |
| Launch defaults | `src/bringup/launch/nuc.launch.py`, `humanoid_arm_skills/launch/humanoid_arm_visual_servoing.launch.py` |
| Navigation config | `src/navigation_stack/{local_planner,route_planner}/config/unitree_g1.yaml` |

track_anything runs on the Orin and its VLM on the Thor. You can read their topics only.

## Topics and TF

| Name | Holds |
|---|---|
| `/track_3d/selected_normal_pose` | Raw target pose, camera frame, +X out of the panel |
| `/track_3d/selected_normal_pose_filtered/arm_upright` | Filtered upright goal the arm is sent, `foot_world` |
| `/track_3d/is_tracking` | The 2D tracker holds a mask; a 3D pose may still be missing |
| `/track_3d/tracked_overlay` | Camera image with mask, box and tracker state |
| `/odom_base_link` | Robot pose in `map` |
| `/route_planner/status` | JSON: `state` (`routing`, `arrived`, `blocked`), `dist_to_goal` |
| `/camera/camera/color/camera_info` | Camera intrinsics |
| TF `pelvis`, `torso_link`, `foot_world` | Body frames; `body_ekf` publishes `foot_world` |
| TF `left_ee`, `right_ee` | End-effector points, 0.1847585 m along X of `*_wrist_yaw_link` |
| TF `camera_link` | Fixed on `torso_link`; the RealSense frames hang below it |
| TF `base_link` | Navigation base: `sensor` (the lidar on the torso) 0.35 m lower, with the lidar's yaw. `navigate_to` puts it on the goal; `get_stance` measures `pelvis`, 1 to 2 cm and about 2° plus the waist yaw away |
| Service `/check_reach` | ik_server: move_to_pose's IK verdict for a pose, without moving the arm; its miss in `pelvis` |

The live target is often gone when you look. The servo node resets track_anything after a cancel, a timeout, a failed seed or a success. After `Planning failed` the tracker may still publish. Trust `current_goal` in the failure record.

## The press pipeline

`press_button` calls the action `humanoid_arm_visual_servoing`:

1. **Seed.** Reset track_anything, then send the button text. The VLM gives a box; the tracker follows it. Each seed request waits 30 s, up to 2 requests.
2. **Target.** `arm_pose_filter` moves the pose into `foot_world`, filters it and publishes the upright goal.
3. **Plan.** move_to_pose plans in `pelvis`: IK for the pre-grasp, a joint-space path to it, a straight final leg to the press pose, then a retreat.
4. **Replan.** It replans when the target moves 0.01 m or 0.05 rad. The final leg freezes the goal in `left_ankle_roll_link` and runs in 4 steps, each re-mapped to the leaning pelvis.
5. **Done.** `Target reached`, then `Controller reached target`. This does not prove the button was pressed (E-018).

## Pose conventions

- **Normal pose** (track_anything): plane centroid, +X out of the panel toward the camera.
- **Upright goal**: same position, yaw-only, +X into the panel. This is `current_goal` and the press pose.
- **Pre-grasp**: the upright goal moved 0.10 m along its own −X. Both poses must be reachable; the pre-grasp can hit the torso when the panel is near.
- **Press offset**: `arm_safety_distance_{x,y,z}` on move_to_pose shifts every target in its own frame (x deeper, y left, z up). `set_press_offset` sets it before every press: 0 for the first press at a panel, then the last `offset_retry`. It resets to 0 on a relaunch.

## Arm facts

- Profile `arms_with_waist` locks the waist to ±0.0001 rad. The torso cannot lean.
- `g1.yaml` `joint_limit_overrides` tightens the shoulder limits. Use them over the URDF's.
- End effector: `offset: [0.1847585, 0, 0]` from `*_wrist_yaw_joint`. `collision_margin: -0.01`.
- The planner solves IK with CasADi in C++. Your pinocchio check uses the same model with another solver, so they can disagree near the workspace edge. Say so when your result is marginal.
- Joints move at most 0.5 rad/s, 0.25 rad/s on the final leg.

## Failure messages

| Message | Meaning |
|---|---|
| `Planning failed: target out of reach: the arm ends X mm and Y deg from it` | Best IK misses by > 5 mm or 0.1 rad; the arm did not move |
| `Planning failed: pre-grasp ... in collision or past the target surface` / `start configuration is in collision` / `no collision-free path to the pre-grasp` | Joint-space search failed. A pre-grasp in collision usually means the robot stands too close |
| `Planning failed: empty trajectory` / `no waypoints` / other text | No trajectory, with no reason or with the exception text |
| `Replan failed: ... (3 attempts)` / `Final-leg step N failed: ... (3 attempts)` | Three plans in a row failed while the arm held still |
| `Arm is not following the trajectory` / `Arm stopped with a joint X rad from its commanded position` | A joint stayed > 0.2 rad off for 1 s, > 2 cm from the target: blocked or not enabled |
| `Target pose transform failed` / `Invalid target pose` | No TF to `pelvis` in 0.5 s, or a non-finite goal |
| `No joint feedback received yet` / `Joint feedback is stale` | Joint state missing or older than 0.5 s |
| `Internal error` / `Timed out braking the previous motion` | Planner exception, or the last motion did not brake within 5 s |
| `No filtered target pose within 10 s of tracking` | Tracked, but no filtered pose reached the arm |
| `move_to_pose action server not available` / `... lost` / `goal rejected` / `send failed` | move_to_pose missing or refused the goal |
| `No target seed after N request(s)` | track_anything never started tracking |
| `Preempted by a newer goal` / `Cancelled` / `Trajectory cleared` | Another goal or a cancel ended it |
| `goal rejected` (orchestrator) | Another servo goal active, or within 1 s of the last success |
| `press_judge` `not_pressed` | A judge looked at the frames and saw no press. Its reason says where the finger sat |
| `check_reach` `target out of reach: the arm ends X mm and Y deg from it; miss in the pelvis frame ...` | The same IK test as the press, from the arm at rest after the stance's mode change and EKF reset (`look_here`), on press_button's goal plus the offset in force |
| `check_reach` `no target pose on ... look first` / `planning failed: ...` | No fresh filtered goal within 2 s, or no plan at all: no miss to walk by |

**Orchestrator timeout.** `press_button` times out at 60 s; a press takes 17 to 25 s. The action's own message is then lost. Read the feedback:

- `distance_to_goal` `inf` throughout: no seed.
- Still falling at 60 s: a slow reach.
- `tracking_lost` true: no filtered target for 5 s.

Feedback comes at 2 Hz. `distance_to_goal` is end effector to upright goal, in `foot_world`.

## Perception facts

- `is_tracking` true means a 2D mask. The 3D pose can still be dropped (too few depth points, failed fit) with no message.
- A lost object shows `Lost (Ns)` in the overlay. After 20 s the tracker stops and does not re-query.
- A seed can lock onto the wrong button (E-003). Walking closer does not fix a wrong target.
- The D435 gives no depth closer than about 0.28 m (E-005).

## Navigation facts

- `navigate_to` succeeds when the local planner reports the robot within `goal_reached_threshold` 0.15 m and `goal_yaw_threshold` 0.15 rad. The `arrived` state on `/route_planner/status` comes earlier (within 2 m lookahead) and is not the arrival.
- A route `blocked` for 10 s fails with `state = blocked for 10 s`: your stance is off the route graph. Blocked from the first feedback with `dist_to_goal` inf points at the wrong storey.
- Goals carry the storey in z (about −0.46 m on the elevator floor). A reposition keeps the base goal's z.
- Walking needs HighLevel mode, `/stop` 0 and `/autonomy_mode` true. The tree sets these around your reposition walk.
- Side and back steps are slower: 0.15 m/s.

## Sources

Facts above were read from these versions. Update this file when they change.

- manipulation_stack `pelvis-correction` @ `d70b9d1`; the NUC runs `test/pelvis-correction` @ `0554dd4`. Files: `humanoid_arm_skills/{humanoid_arm_visual_servoing,pose_filtering,utils}.py`, `g1_pilot/src/move_to_pose/{server,planning,control}.cpp`, `g1_pilot/src/core/arm_mp/{visual_servo_planner,joint_space_planner}.cpp`, `g1_pilot/config/g1.yaml`, `g1_description/assets/g1/*.urdf`
- track_anything `main` @ `f5a1f47`: `track_3d.py`, `track_3d_parameters.yaml`, `launch/track_3d.launch.py`
- navigation_stack `main` @ `6242b3e`: `route_planner_node.py`, `local_planner.cpp`, `{local_planner,route_planner}/config/unitree_g1.yaml`
- NUC: `src/bringup/launch/nuc.launch.py`
- orchestrator: `orchestrator/agent.py`, `agent_push.py`, `skill_registry/contracts/{press_button,set_press_offset,manipulation_recovery}.yaml`, `skill_registry/trees/freight_elevator_go_to_place.xml`
