# Sim hand chaining (FVD elevator task)

A hand-written task chain for the FVD (Fall Validation Demonstration) elevator task (Test 10) in the MuJoCo sim. It is the baseline for the orchestrator's LLM-planned behavior trees. The chain is fixed Python code, not a behavior tree.

The command `start floor:1; stop floor:2; target: door 1` makes the G1 do these steps:

1. Receive the command, set the navigation parameters for the press poses.
2. Localize in the prior map (GICP (Generalized Iterative Closest Point) from the initial guess; if no match, a particle filter, then GICP).
3. Walk to the call panel and press the call button (up or down).
4. Walk into the car when the landing door opens.
5. Press the floor button.
6. Ride, detect the car door open, relocalize on the new floor, walk out.
7. Walk to the target.

Each step has a timeout and bounded retries. Status goes to `/task_executor/status` (JSON).

## Results

Runs 47 to 51 (2026-10-28, the code in this folder): 5 of 5 done, 148 to 179 s from command to target. Settings: `locomotion:=nav`, `button_source:=track_3d` (button pose from Thor's perception), robot put down at `[3.6, -3.5, -2.094]` (2.5 m and 150 deg from the annotated start, so the particle filter runs), floor 1 -> floor 2, target `door 1`.

## Contents

The layout is the same as `~/eigenvector_ws/fvd_sim/jean_dev` on vector-pc. The scripts use paths relative to that folder; do not move the files.

| Path | What it is |
|---|---|
| `src/hand_chaining/task_executor.py` | The task chain (ROS 2 node `task_executor`) |
| `src/hand_chaining/pf_relocalize.py` | Particle filter relocalization (global search when GICP rejects the guess) |
| `src/door_state/` | Lidar door state: counts scan points in a rectangle behind the door |
| `src/hand_chaining/sim/sim_orin.py` | Simulated Orin and lidar: the elevator_sim MuJoCo world behind the G1's ROS 2 interfaces |
| `src/hand_chaining/sim/mapping_tour.py` | Mapping operator for the sim (makes the prior map) |
| `config/fvd_sim.launch.py` | Sim + NUC (Next Unit of Computing) navigation stack + door state + particle filter + IMU (Inertial Measurement Unit) floor counter + executor |
| `config/fvd_sim_places.yaml` | All annotated poses, buttons, press regions, car geometry |
| `config/nav_params_g1_press.yaml` | Navigation parameters the executor sets at each command |
| `config/fvd_sim_door_*.yaml` | Door state rectangles |
| `config/elevator_lio/` | Elevator-LIO (Lidar-Inertial Odometry) configs for mapping |
| `config/vector_pc.env` | Shell environment (ROS domain 73, localhost only) |
| `tool/hand_chaining/run_fvd.sh` | One full run in tmux: stack, bag, command, stop |
| `tool/hand_chaining/thor_perception.sh` | Start or stop the button perception on Thor |
| `tool/hand_chaining/*.py` | Test and analysis tools (press reach sweep, nav approach test, grounding test, run video, run report, map tools) |

Not in this folder: the prior map `maps/sim_map_world_clean.pcd` (5 MB). Make it with `mapping_tour.py`, then `tool/hand_chaining/register_map.py` and `clean_map.py`.

## Run

On vector-pc, with this folder at `~/eigenvector_ws/fvd_sim/jean_dev`:

```bash
ssh vector@vector-thor 'bash -s -- start' < jean_dev/tool/hand_chaining/thor_perception.sh
```

```bash
cd ~/eigenvector_ws/fvd_sim && jean_dev/tool/hand_chaining/run_fvd.sh run52
```

Use `button_source:=sim` as a third argument to read the button pose from the sim (no Thor).

## Skills and where they come from

The executor uses the robot's interfaces only. The names are the same as in the orchestrator's skill contracts (`skill_registry/contracts/`). In the sim, `sim_orin.py` serves the Orin-side interfaces.

| Skill (chain step) | Interface | Provided by |
|---|---|---|
| localize (2, 6) | `/initialpose` -> `/slam_elevatorlio/status` | `slam_elevatorlio` in navigation_stack |
| global relocalize (2) | `/pf_relocalize/request` -> `/pf_relocalize/status` | `src/hand_chaining/pf_relocalize.py` (this folder) |
| navigate_to (3, 4, 6, 7) | `/route_goal`, `/cancel_goal`, `/speed`, `/autonomy_mode`, `/goal_reached`, `/route_planner/status` | route planner, local planner, path follower in navigation_stack |
| own moves (step back, turn, align, straight walk) | `/local_movement_cmd_vel` | the navigation stack's command mux; only with `locomotion:=own` |
| set_hand (3, 5) | `/unitree_interface/set_hand_pose` (POINT, right hand) | unitree_interface; in the sim, answered with no effect (closed Dex1-1 gripper) |
| press_button (3, 5) | `humanoid_arm_visual_servoing` action (`VisualServoingArm`, right arm) | interface from manipulation_stack; in the sim, `sim_orin.py` plans with G1Pilot (elevator_sim + its manipulation_stack copy) |
| button pose for the press | `/track_3d/reset`, `/keyboard_input` -> `/track_3d/selected_normal_pose` | Thor: vlm (Cosmos GroundBoxes, seed_bbox) + track_anything (track_3d, EdgeTAM) |
| release_arms (3, 5) | `/unitree_interface/release_arms` | unitree_interface; in the sim, `sim_orin.py` |
| button_lit (3, 5) | `/sim/button_lit` | sim stand-in (reads the ring state). `button_check:=vlm` uses `orchestrator/vlm_client.py` on Thor instead (not tested in these runs) |
| door_open (4, 6) | `/door_state/landing`, `/door_state/car` | `src/door_state/door_state.py` (this folder) |
| floor counter (5) | `/elevator/floor` | `imu_floor_detect_node` in elevator_states |

### Versions used (runs 47 to 51)

| Repo | Branch | Commit | Local changes |
|---|---|---|---|
| [navigation_stack](https://github.com/EigenVectorRobotics/navigation_stack) | `main` | `6242b3e1edebb93f8416d78e5f51b9f21045ff58` | `terrain_analysis/config/unitree_g1.yaml`: `no_decay_distance` 0.3 -> 0.0 (not committed) |
| [manipulation_stack](https://github.com/EigenVectorRobotics/manipulation_stack) (interfaces, in `fvd_sim/src`) | `planner/roadmap-tiered-replan` | `809f7a0ca573815220d7982cff01270962b5e1ef` | none |
| [elevator_sim](https://github.com/EigenVectorRobotics/elevator_sim) | `main` | `035dde74b73204300615c34b27ac088799295bfe` | Not committed: Dex1-1 gripper in place of DEX3 (`scripts/prepare_dex1.py`, `assets/robots/dex1/`, `scene.xml`, `scene_metadata.json`, `integrations/manipulation/g1_sim.yaml` TCP offset 0.1847585 m), press goal moves with the car (`elevator_sim/agent/manipulation.py`), cabin panel texture |
| manipulation_stack (G1Pilot, `elevator_sim/.deps/manipulation_stack`) | detached | `b08d78a1c42b7d57302604d2e4a67c374ef047f0` | Not committed: Dex1-1 meshes and URDF (`g1_description/assets/g1/`) |
| [unitree_interface](https://github.com/EigenVectorRobotics/unitree_interface) | `main` | `f25013a8b9457ccf1c91ffb667fd3ceaa7ed47db` | none |
| [elevator_states](https://github.com/EigenVectorRobotics/elevator_states) | `main` | `3a40bceee84be2ec8bbc4d8f0ac94b1991c772f7` | none |
| [livox_ros_driver2](https://github.com/EigenVectorRobotics/livox_ros_driver2) | `master` | `7e4114aa24c8ccbe1d5a7687e2bccda2c22e587e` | none |
| vlm (on Thor, `~/eigen_vector_ws/install_cosmos`) | `thor` | not recorded | as deployed on Thor; nothing copied |
| track_anything (on Thor, `~/eigen_vector_ws/install`) | not recorded | not recorded | as deployed on Thor; nothing copied |

## Hard-coded values

All values are for the sim building. Most need new values on the real robot.

### Elevator (sim)

| Value | Where |
|---|---|
| Car door stays open 15 s, then closes when the doorway is clear (`door_hold_s`) | `config/fvd_sim.launch.py`, `sim_orin.py` |
| Light curtain: a closing door opens again when the pelvis is in `abs(x) < 0.9`, `-0.5 < y < 0.8` | `sim_orin.py` `_doors` |
| Door speed 0.4 stroke/s (about 2.5 s to open), 2 s dwell before a queued call, button light on for at least 8 s | elevator_sim `environment.py` |
| Car 0.65 m/s, 0.6 m/s^2, floor height 3.8 m (about 5.8 s for one floor) | elevator_sim `environment.py` |
| Floor ground z: floor 1 at 0.0 m, floor 2 at 3.8 m. Floor labels `[B, A, 1, 2, 3, 4]` | `config/fvd_sim_places.yaml` |
| IMU floor counter: `accel_threshold` 0.03 g, `invert_z`, `duration_cutoffs` `[0, 8.5, 14.5, 20.5, 26.5, 32.5]` s | `config/fvd_sim.launch.py` |
| Button lit check from `/sim/button_lit` (sim truth, not a VLM (Vision-Language Model)) | `task_executor.py` `button_lit` |

### Places (map frame, `config/fvd_sim_places.yaml`)

| Value | Notes |
|---|---|
| Start guess `(1.6, -2.0, 90 deg)` on each floor | initial guess for GICP |
| Call panel press pose `(0.550, -0.517, 61.8 deg)` | aim minus the mean arrival error of the last leg |
| Car panel press pose `(-0.879, 0.974, -93.0 deg)` | same |
| Car inside point `(-0.55, 1.6)`, exit point `(0.8, -1.3, -60 deg)` | straight walks in and out (`locomotion:=own`); exit is the nav goal out of the car |
| Car floor rectangle `[-1.25..1.25] x [0.5..2.55]` | `in_car` check |
| Targets: `door 1` `(4.5, -5.5, 0)`, `door 2` `(-4.5, -5.5, 180 deg)` | |
| Hall buttons at `(0.735, -0.134)`, floor buttons at `(-0.99, 0.451)`, panel normals | only for the reach check; the press finds the button with its own perception |
| Press regions per button (distance from the panel, lateral offset, turn) | from `press_reach_sweep.py`; outside it the robot walks the last leg again |
| Grounding queries for `floor_1`, `floor_2`: `object:the small round push button at the right end of the dark plate labeled N` | the plain name boxed the cap in 0 of 15 frames; hall buttons use `up` / `down` |
| Map masks: the closed door leaves at both doorways | removed from the prior map by `clean_map.py` |
| Robot put down at `[3.6, -3.5, -2.094]` | `run_fvd.sh` default |

### Door state (`config/fvd_sim_door_*.yaml`)

| Value | Notes |
|---|---|
| Landing view: rectangle `[-1.0..1.0] x [0.8..2.3]` (inside the car) | door open when it sees points inside the car |
| Car view: rectangle `[-0.8..0.8] x [-1.8..-0.4]` (on the landing) | door open when it sees points on the landing |
| 36 points, minimum intensity 5 | threshold for OPEN |
| OPEN or CLOSED must hold 1 s | `task_executor.py` `on_door`, `door_held` |

### Navigation overrides (`config/nav_params_g1_press.yaml`, set at each command)

`local_planner`: `goal_reached_threshold` 0.06 m, `goal_yaw_threshold` 0.1 rad, `goal_clear_range` 0.2 m, `goal_behind_range` 0.0 m. `path_follower`: `stop_distance_threshold` 0.03 m, `slow_down_distance_threshold` 0.25 m, `two_way_drive` true, `omni_direction_goal_threshold` 0.25 m, `min_turn_yaw_rate` 18 deg/s, `min_turn_deadband` 0.08 rad. The files of navigation_stack are not changed.

### Executor timing and motion (`task_executor.py`)

Parameters (default value):

| Parameter | Default | Use |
|---|---|---|
| `max_press_attempts` | 3 | presses per button |
| `nav_timeout_s` | 180 s | one navigation goal |
| `door_timeout_s` | 40 s | door opens after a press |
| `align_tolerance_m`, `align_tolerance_deg` | 0.07 m, 5 deg | final approach (`locomotion:=own`) |
| `sensor_height_m` | 1.23 m | z of the initial guess |
| `walk_speed`, `door_speed` | 0.5, 0.4 m/s | normal walk; walk in and out of the car |
| `step_back_m`, `car_step_back_m` | 0.3, 0.6 m | `locomotion:=own` |
| `nav_step_back_m`, `nav_return_speed` | 0.3 m, 0.2 m/s | step back after a hall press; last leg to a press pose |
| `release_wait_s` | 5 s | wait after `release_arms` before the body moves |
| `stop_lead_s` | 0.3 s | the gait walks on this long after a stop |
| `button_check` | `topic` | `none`, `topic` (sim), `vlm` |
| `vlm_base_url` | `http://vector-thor:8090/v1` | only with `button_check:=vlm` |

Constants in the code:

| Value | Where |
|---|---|
| Press: arm goal accepted in 10 s, result in 90 s; right arm only, POINT hand | `press` |
| After the floor press: car door closes in 30 s, then opens in 90 s | `car_arrives` |
| The IMU floor change must equal the expected change; if the counter saw no ride, the door cycle alone says arrived | `car_arrives` |
| Localize: 2 GICP attempts, then 2 particle filter attempts (sigma 1.5 m, 180 deg); 30 s each; stop after 4 rejected matches | `localize`, `wait_localized` |
| Navigation: done on `/goal_reached` within 0.4 m; fail if the route is blocked for 20 s or SLAM diverges | `navigate` |
| Settle: still for 1.5 s (0.01 m, 0.01 rad), at most 15 s | `settle` |
| Final approach: 0.15 to 0.25 m/s, yaw rate up to 0.4 rad/s, turn in place first when off by more than 20 deg, 3 moves | `align` |
| Turn in place: 0.3 to 0.8 rad/s, 8 deg tolerance | `turn_to` |
| Straight walk: pure pursuit 0.4 m ahead, done within 0.15 m (in) and 0.25 m (out) | `walk_straight`, `enter_car`, `exit_car` |
| Enter and exit: 2 attempts; a door that closes first means call again (enter) or press the floor button (exit) | `enter_car`, `exit_car` |
| Reach check: 3 attempts | `reach` |
| Odometry older than 5 s is a failure | `current` |

### Network (`tool/hand_chaining/*.sh`, `config/vector_pc.env`)

ROS domain 73, localhost discovery. Thor at `100.94.122.122` and vector-pc at `100.118.78.37` (tailscale), joined with `ROS_STATIC_PEERS`.
