# Deployment

What runs where on the Stanley G1's computers: how to reach each one, the workspace in use, the branch of each repository, and what exists only on a machine. This file is a snapshot. **Checked: 2026-11-08, evening.** Check it again (the commands at the end) after every deploy, and update the tables.

How to bring the machines up and run a task is in `docs/operations.md`.

## Machines

| Machine | Runs | Reach it | OS, ROS | Workspace in use |
|---|---|---|---|---|
| NUC | robot interface, arm control, body EKF, navigation, `door_state_node`, the orchestrator | `ssh vector@vector-nuc` (tailscale); `192.168.123.5` on the robot wire | Ubuntu 24.04, Jazzy | `~/eigen_vector_ws` (stack), `~/orchestrator_ws` (orchestrator, an overlay on it) |
| Orin | camera (`realsense2_camera`, apt), `seed_bbox`, `track_3d`, gripper pose estimator | from the NUC: `ssh unitree@192.168.123.164` (the NUC has the key) | Ubuntu 22.04, Humble | `~/eigen_vector_ws` |
| Thor | Cosmos daemon (docker container `edgellm`), `ground_server`, `cosmos_vlm_server` | from the NUC: `ssh vector@192.168.123.1` (the NUC has the key); `vector-thor` over tailscale | Ubuntu 24.04, Jazzy | `~/eigen_vector_ws_bringup` (stack), `~/orchestrator_ws` (only for `cosmos_vlm_server`) |

- `env.sh` in each workspace is written by `eigen_vector/scripts/deploy.sh`; source it before any ROS command on that machine. On the NUC, `~/orchestrator_ws/env.sh` sources the stack's environment too.
- Passwords are not kept in the repository: ask the team. From the laptop, the Orin and the Thor are reached most reliably through the NUC (`ssh vector@vector-nuc "ssh unitree@192.168.123.164 '...'"`); `ssh -J` to them has timed out.
- `deploy.sh launch orin` reaches the Orin at `192.168.123.164` through the NUC, so the laptop needs that address's current host key in `known_hosts`.

## Repositories on each machine

Branch @ commit, and local changes, as checked. "No upstream": the local branch tracks no remote branch. A row marked **to deploy** names the branch the team runs from now on; what was checked is in its last column until the machine is updated.

### NUC, `~/eigen_vector_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `bringup` | not a git checkout: copied by `deploy.sh`, then edited by hand (see "Only on a machine") | |
| `manipulation_stack` | **to deploy:** `main` (`4f12ae1` when checked: the gripper estimation merged as #10; contains `e63f2b8`, body_ekf `~/reset`) | checked: `cam-gripper-estimation` @ `ba0cbfa` with 6 files changed, not committed (`g1_pilot/src/move_to_pose/{planning.cpp,server.cpp,server.h}`, `humanoid_arm_skills/humanoid_arm_skills/gripper_pose_estimator.py`, `humanoid_arm_skills/launch/humanoid_arm_visual_servoing.launch.py`, `humanoid_arm_skills/package.xml`) |
| `navigation_stack` | `jean/elevator-side` @ `39e6139` | no upstream; one untracked file, `slam_elevatorlio/slam_elevatorlio/slam_elevatorlio_parameters.py` |
| `elevator_states` | `jean/door-state-in-out` @ `8ad0552` | no upstream; clean |
| `unitree_interface` | `jean/fsm-id-topic` @ `e234864` | no upstream; clean |
| `livox_ros_driver2` | `master` @ `7e4114a` | clean |
| `rmw_cyclonedds` | `jazzy` @ `2659b2b` | clean |

### NUC, `~/orchestrator_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `orchestrator` | **to deploy:** `ltx-dev` | checked: `jean/planner-tree-library` @ `e99f402` |

### Orin, `~/eigen_vector_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `bringup` | not a git checkout (copied 2026-11-02) | |
| `manipulation_stack` | `cam-gripper-estimation` @ `ba0cbfa` | 2 files changed, not committed |
| `track_anything` | `perf/track3d-latest-frame` @ `191b222` | no upstream; 7 files changed, not committed |
| `vlm` | `main` @ `3ca4861` | clean |
| `unitree_interface` | `main` @ `5e3257a` | clean |
| `rmw_cyclonedds` | `humble` @ `e370e09` | clean |

### Thor

| Workspace | Repository | Branch @ commit | Local state |
|---|---|---|---|
| `~/eigen_vector_ws_bringup` | `bringup` | not a git checkout | |
| | `vlm` | `main` @ `3ca4861` | clean; `ground_server` runs from here |
| | `rmw_cyclonedds` | `jazzy` @ `2659b2b` | clean |
| `~/orchestrator_ws` | `orchestrator` | **to deploy:** `ltx-dev` | checked: `tingxual/vec-125-scope-the-agentic-reasoning-topic` @ `33af8fd`, a branch deleted on GitHub. `launch_thor_stack` starts `cosmos_vlm_server.py` from this checkout |
| `~/eigen_vector_ws` | `track_anything` `trt` @ `f5a1f47`, `vlm` `thor` @ `3ca4861` | | not in use |

Running when checked: on the Thor, the tmux session `eigen_vector_bringup` (`ground_server`); no `cosmos_vlm` session, so the VLM endpoint `http://vector-thor:8090/v1` did not answer. The Thor rebooted on its own on 2026-11-08 at about 21:20 and came back with no stack (troubleshooting, "Machines and processes").

## Only on a machine (not in any repository)

| Machine | Path | What | Status |
|---|---|---|---|
| NUC | `~/eigen_vector_ws/src/bringup/config/cyclonedds.xml` | `MaxAutoParticipantIndex` 120 (N-001) | needed; not committed to `eigen_vector` |
| NUC | `~/eigen_vector_ws/src/bringup/launch/nuc.launch.py` | includes `elevator_states`' `door_state_node.launch.py` | on `eigen_vector` branch `jean/door-state-bringup`, not on `main`. The bringup redeployed on 2026-11-08 at 20:29 no longer includes it: `door_state_node` ran in its own tmux session `door_state` (`docs/operations.md`, section 5) |
| NUC | `~/nuc_manip_only.launch.py`, `~/nuc_nav_only.launch.py` | the NUC stack split in two: robot interface, arm control and foxglove (`tmux -L eigen_vector`); navigation and SLAM (`tmux -L nav`, log `~/nav_launch.log`) | what ran on 2026-11-08 evening; not in any repository |
| NUC | `~/maps/two_floor_clean_cabin2.pcd`, `~/maps/two_floor_clean_cabin2.places.yaml` (backups `*.bak_*`) | the map in use and its places and areas (`freight elevator cabin`, `freight elevator hall`) | the executor, `slam_elevatorlio` and `door_state_node` read the places file at start |
| NUC | `~/fake_button_pose.py` | fixed button poses for `press_button` with no Thor | disabled: do not start it |
| NUC | `~/watch_records_*.sh`, `~/ekf_reset.sh` | an old EKF reset outside the trees | not used: `ekf_hold` calls `reset_ekf` |
| NUC | `~/places_markers.py` | publishes the places as markers for Foxglove (`tmux -L places`) | optional; stops with a reboot |
| NUC | `~/orch_runs/` | run records: one folder per bring-up session, `runs/<n>-<tree>/` each | the operator clears old runs by hand |

## Not in use

Other workspaces on the machines; nothing that runs comes from them.

- NUC: `eigen_vector_orch_ws`, `eigen_vector_ws_apt`, `eigen_vector_ws_lio`, `fsm_check_ws`, `nav_fix_ws`, `scratch_ekf_tf_ws`, `unitree_test_ws`
- Orin: `eigen_vector_scratch_ws`, `eigen_vector_ws_nav`, `ev_humble_ws`, `humble_ws`, `thor_aligned_ws`
- Thor: `eigen_vector_ws`, `scratch_pixi_ws`, `scratch_unitree_ws`

## Check it again

On each machine, for each workspace: the branch, the commit, and the local changes of every repository under `src/`.

```bash
for d in ~/eigen_vector_ws/src/*/; do
  git -C "$d" rev-parse --git-dir >/dev/null 2>&1 || { echo "$(basename $d) | not git"; continue; }
  echo "$(basename $d) | $(git -C $d rev-parse --abbrev-ref HEAD) @ $(git -C $d log -1 --format=%h) | changed $(git -C $d status --porcelain | wc -l)"
done
```
