# Machines

The Stanley G1's computers in two parts: what runs where, as last checked on the machines (a snapshot), and what the NUC, the Orin, the Thor and the operator laptop must change for the orchestrator on `main`, with the contracts in this repository that check each change. How to bring the machines up and run a task is in [run.md](run.md).

## What runs where (snapshot)

What runs where: how to reach each machine, the workspace in use, the branch of each repository, and what exists only on a machine. This part is a snapshot. Check it again (the commands at the end of this part) after every deploy, and update the tables.

### How to read it

Three kinds of statement, marked as such:

- **Checked (2026-11-22, 15:10):** the NUC and the Orin, seen on the machine then, read only. Both rebooted at 13:09. The Orin's `~/eigen_vector_ws` was switched to `main` in every repository and rebuilt from clean (15:01 to 15:03); on the NUC, `manipulation_stack` was switched to `main` and its four packages rebuilt (15:05), and the orchestrator was pulled to `main`. No stack ran on the NUC when checked, and none has run on these builds yet (re-check question 3). Everything about the NUC and the Orin not marked otherwise is of this kind.
- **Checked (2026-11-19, 21:30):** the Thor, seen on the machine then and not looked at since.
- **Checked (2026-11-08):** seen then and not looked at since; marked where it appears.
- **GitHub (2026-11-17):** read from the repositories on GitHub, not from a machine.

What to check on the robot is in "Re-check on the robot", as questions.

### Machines

| Machine | Runs | Reach it | OS, ROS | Workspace in use |
|---|---|---|---|---|
| NUC | robot interface, arm control, body EKF, navigation, `door_state_node`, the orchestrator | `ssh vector@vector-nuc` (tailscale); `192.168.123.5` on the robot wire | Ubuntu 24.04, Jazzy | `~/eigen_vector_ws` (stack), `~/orchestrator_ws` (orchestrator, an overlay on it) |
| Orin | camera (`realsense2_camera`, apt), `seed_bbox`, `track_3d`, gripper pose estimator, Dex1 gripper server | from the NUC: `ssh unitree@192.168.123.164` (the NUC has the key) | Ubuntu 22.04, Humble | `~/eigen_vector_ws` (stack), `~/eigen_vector_orch_ws` (only the Dex1 gripper server) |
| Thor | Cosmos daemon (docker container `edgellm`), `ground_server`, `vlm_server` | from the NUC: `ssh vector@192.168.123.1` (the NUC has the key); `vector-thor` over tailscale | Ubuntu 24.04, Jazzy | `~/eigen_vector_ws_bringup` (stack), `~/orchestrator_ws` (only for `vlm_server`) |

- `env.sh` in each workspace is written by `eigen_vector/scripts/deploy.sh`; source it before any ROS command on that machine. On the NUC, `~/orchestrator_ws/env.sh` sources the stack's environment too.
- Passwords are not kept in the repository: ask the team. From the laptop, the Orin and the Thor are reached most reliably through the NUC (`ssh vector@vector-nuc "ssh unitree@192.168.123.164 '...'"`); `ssh -J` to them has timed out.
- `deploy.sh launch orin` reaches the Orin at `192.168.123.164` through the NUC, so the laptop needs that address's current host key in `known_hosts`.
- A build by hand uses the arguments `deploy.sh` uses: `COLCON_ARGS` in `eigen_vector` `config/nuc.env` and `config/orin.env` (`--symlink-install` and `CycloneDDS_DIR`), `config/thor.env` (`--symlink-install`). On the NUC, never with the robot standing ([run.md](run.md#2-deploy-on-the-nuc)): one worker under a standing robot is still forbidden; with the robot in the gantry, all cores is fine (the clean NUC build then takes under 15 minutes). The rebuild of 2026-11-19, from each workspace (NUC and Orin `~/eigen_vector_ws`, Thor `~/eigen_vector_ws`), after `rm -rf build install log` and `source env.sh`:

  ```bash
  colcon build --base-paths src --symlink-install --cmake-args -DCycloneDDS_DIR=$HOME/eigen_vector_ws/src/unitree_interface/cmake/unitree_cyclonedds   # NUC
  colcon build --base-paths src --symlink-install --packages-skip g1_pilot --cmake-args -DCycloneDDS_DIR=$HOME/eigen_vector_ws/src/unitree_interface/cmake/unitree_cyclonedds   # Orin
  colcon build --base-paths src --symlink-install   # Thor
  ```

  Then the orchestrator overlay on the NUC, also from clean ([run.md](run.md#2-deploy-on-the-nuc)).
- **Do not run `deploy.sh build` or `install` on these workspaces as they are:** its `bringup_copy` (`eigen_vector` `scripts/provision.sh`) still copies `bringup` to `src/bringup`, which would be a second package `bringup` next to `src/eigen_vector/bringup`.
- `git fetch` on the NUC and the Orin needs your SSH agent: `ssh -A` to the NUC, and `ssh -A` again from there to the Orin.

### Repositories on each machine

Branch @ commit, and local changes, as checked on 2026-11-22 (the NUC and the Orin) and 2026-11-19 (the Thor). A stash named in a row holds the local changes the machine had before the branch in the row was checked out; `git stash list` in that checkout shows it.

**bringup comes from the `eigen_vector` checkout in `src/`.** Since 2026-11-19 the NUC and the Orin have the repository `eigen_vector` cloned as `src/eigen_vector`, and colcon builds the package `bringup` from its `bringup/` directory; there is no copied `src/bringup` any more. The Thor's running workspace `~/eigen_vector_ws_bringup` still has a copied `bringup` (below). The separate repository `EigenVectorRobotics/bringup` stopped at 2026-10-27 (`4c70a72`): its `nuc.launch.py` starts only the arm visual servoing, so `nuc_up` cannot pass with it. Do not deploy from it.

#### NUC, `~/eigen_vector_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `eigen_vector` | `main` @ `3219b75` (pulled 2026-11-22; the commits since `9306407` change only the Orin) | clean. `bringup/launch/nuc.launch.py` includes `door_state_node.launch.py` and has no `floor_map_selector`; `bringup/config/cyclonedds.xml` has `MaxAutoParticipantIndex` 120 |
| `manipulation_stack` | `main` @ `96b2ca2` (switched from `fix/button-press-aim` @ `e710393` on 2026-11-22, whose commits are all in `main`; `g1_description`, `humanoid_manipulation_interfaces`, `g1_pilot` and `humanoid_arm_skills` rebuilt 15:05) | clean. `main` carries the hand-tuned D435 mount (0.068963, 0.023804, 0.418953) and publishes the target `move_to_pose` plans for on `~/planning_target` |
| `navigation_stack` | `main` @ `4cf9d87` | no tracked file changed; 32 untracked backup copies (`*.pre_*` and the like) |
| `elevator_states` | `main` @ `f6261be` | **`f6261be` is a local commit, not on GitHub** (2026-11-19 22:45, "door areas of elevator 1"): `config/elevator_door_state.yaml` `places_file` is `/home/vector/maps/fvd_map_clean_elevator1.places.yaml` (see "Only on a machine"). No file changed beyond it |
| `unitree_interface` | `main` @ `ffb78c3` | clean; matches `origin/main` |
| `livox_ros_driver2` | `master` @ `7e4114a` | clean |
| `rmw_cyclonedds` | `jazzy` @ `2659b2b` | clean |

#### NUC, `~/orchestrator_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `orchestrator` | `main` @ `b5e8cca` (matches `origin/main`; built 2026-11-19 21:06 with `--symlink-install`, so a pull of Python, contracts or trees needs no rebuild) | no tracked file changed, no stash. Three untracked test trees, not in the repository: `freight_call_panel_floor1_up_down_press.xml`, `freight_call_panel_up_down_loop.xml`, `freight_call_panel_up_down_recover.xml` |

#### Orin, `~/eigen_vector_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `eigen_vector` | `main` @ `3219b75` (switched from `fix/orin-depth-848x480` on 2026-11-22; `main` has the FoundationPose changes) | clean. `bringup/launch/orin.launch.py` starts `gripper_pose_estimator`, and puts `src/FoundationPose` on its `PYTHONPATH` |
| `manipulation_stack` | `main` @ `96b2ca2` | clean. The stash "orin: main worktree ... before fix/button-press-aim 2026-11-19" (an alternative wrist-pitch search, `fit_pitch`) was not looked at again after the switch to `main`. `g1_pilot` is not built: it needs `casadi`, not installed on the Orin, and the Orin runs no `g1_pilot` node |
| `FoundationPose` | `main` @ `5285bb8` | clean. Not a colcon package: the estimator imports it through `PYTHONPATH`. Its TensorRT plan `models/foundationpose/refine_fp16.plan` was built on the Orin on 2026-11-21 and is not in the repository |
| `track_anything` | `main` @ `4d1c755` | clean |
| `vlm` | `main` @ `196d071` | clean |
| `unitree_interface` | `main` @ `599e2ff` | clean; 2 commits behind `origin/main` (re-check question 4) |
| `rmw_cyclonedds` | `humble` @ `e370e09` | clean |

The checkouts match `eigen_vector` `repos/orin.repos`. Rebuilt from clean on 2026-11-22 (15:01 to 15:03, 10 packages, `g1_pilot` skipped) with the Orin command under "Machines".

`~/eigen_vector_orch_ws` holds only `dex1_1_service`: the systemd unit `dex1_gripper` runs `~/eigen_vector_orch_ws/install/dex1_1_service/bin/dex1_1_gripper_server --network enP8p1s0` (active when checked). Nothing else runs from that workspace.

#### Thor

| Workspace | Repository | Branch @ commit | Local state |
|---|---|---|---|
| `~/eigen_vector_ws_bringup` | `bringup` | not a git checkout (copied) | the workspace that runs: `launch_thor_stack` and the bringup start from it. Not updated or rebuilt on 2026-11-19 |
| | `vlm` | `main` @ `3ca4861` (2026-10-29) | clean; `ground_server` runs from here |
| | `rmw_cyclonedds` | `jazzy` @ `2659b2b` | clean |
| `~/eigen_vector_ws` | `track_anything` | `main` @ `4d1c755` | clean; updated and rebuilt from clean on 2026-11-19 |
| | `vlm` | `main` @ `196d071` | clean; updated and rebuilt from clean on 2026-11-19. Nothing runs from this workspace (re-check question 7) |
| `~/orchestrator_ws` | `orchestrator` | `ltx-dev` @ `8d8fe7d` (`ltx-dev` is deleted on GitHub; `8d8fe7d` is in `main`) | has no `orchestrator/orchestrator/vlm/server.py`, so `launch_thor_stack` cannot start `vlm_server` from it (item 12) |

Running when checked (2026-11-19, 21:30): the tmux sessions `eigen_vector_bringup` and `cosmos_vlm`, both started at 19:42. Earlier that day the Thor had booted at 15:34 and ran no stack: no `eigen_vector_bringup` session, no `vlm_server` or `cosmos_vlm` session; the container `edgellm` came up with the bringup started by hand at 16:28. The VLM endpoint `http://vector-thor:8090/v1` was not started. The Thor also came back with no stack after it rebooted on its own on 2026-11-08 ([troubleshooting.md](troubleshooting.md), "Machines and processes").

### Only on a machine (not in any repository)

| Machine | Path | What | Status |
|---|---|---|---|
| NUC | `~/eigen_vector_ws/src/elevator_states/config/elevator_door_state.yaml` | `places_file: /home/vector/maps/fvd_map_clean_elevator1.places.yaml`, the door areas of elevator 1, in the local commit `f6261be` (not on GitHub). On `origin/main` (`d71ad3d`) it is `two_floor_clean_cabin2.places.yaml`. For the freight elevator it must be `fvd_map.places.yaml` (as on 2026-11-19), with the areas `freight elevator cabin` and `freight elevator hall` | set 2026-11-19 22:45 by a teammate. Change it with the map ([The map](run.md#the-map)), then build `elevator_states` |
| NUC | `~/nuc_manip_only.launch.py`, `~/nuc_nav_only.launch.py` | the NUC stack split in two (what ran on 2026-11-08) | not used on 2026-11-19: the stack ran from one `nuc.launch.py` |
| NUC | `~/maps/<map>.pcd`, `~/maps/<map>.places.yaml` (backups `*.bak_*`) | the maps and their places files; the one in use: [The map](run.md#the-map). The freight elevator is `fvd_map` (its places come from `two_floor_clean_cabin2`; the executor uses `fvd_map_callpanel_tilt.places.yaml`, item 8); `fvd_map_clean_elevator1` and `fvd_map_clean_elevator2` are two other elevators | the executor, `slam_elevatorlio` and `door_state_node` read the places file at start |
| NUC | `~/fake_button_pose.py` | fixed button poses for `press_button` with no Thor | still there (2026-11-19), disabled: do not start it |
| NUC | `~/watch_records_20261108.sh`, `~/ekf_reset.sh` | an old EKF reset outside the trees | still there (2026-11-19), not used: `ekf_hold` calls `reset_ekf` (item 6) |
| NUC | `~/run_viewer/` | the old hand copy of the Run Viewer | still there (2026-11-19); preflight starts the viewer from this repository (item 10) |
| NUC | `~/places_markers.py` | publishes the places as markers for Foxglove (`tmux -L places`) | optional; stops with a reboot |
| NUC | `~/orch_runs/` | run records: one folder per bring-up session, `runs/<n>-<tree>/` each | the operator clears old runs by hand |

### After the `main` deploy

Paths the orchestrator on `main` creates or uses on the NUC.

| Path | What | Seen (2026-11-19) |
|---|---|---|
| `~/orch_state/press_references.jsonl` | the presses judged pressed, for stance suggestions; the old `~/.ros/orchestrator_press_references.jsonl` is read while only it exists, and copied here on the first save (the old file stays) | neither file exists yet: no press has been judged pressed |
| `~/bags/` | the site's raw bag (`raw_bag` in `orchestrator/config/sites/stanley.yaml`), recorded by the launch | yes, `raw_<date>_<time>/`; it closes only when the orchestrator stops with Ctrl-C |
| `~/orchestrator.log` | the orchestrator's log when started as in [run.md](run.md#3-start-the-orchestrator); the executor logs the agent bridge link there, and `start.sh` and `status.sh` read it | yes |
| tmux `run_viewer` | the Run Viewer, started by preflight (`launch_run_viewer`) from `~/orchestrator_ws/src/orchestrator/run_viewer/` | not checked |

### Re-check on the robot

Questions the next check must answer. Each is open until someone looks on the machine.

1. Orin and NUC: does the press capture now take the gripper estimate on most presses? On 2026-11-19 `gripper_pose_estimator` fitted the right gripper about once a second with it in view (inliers 0.41 to 0.64; few left fits) and `move_to_pose` applied the held median (8 to 18 mm up), but only 4 of 33 judged presses recorded `residual_from` other than `encoders`, all on the down button; `8be696a` makes the capture use the same held median. The operator-marked fingertip still sat about 15 mm above the estimate.
2. Orin: where do the CycloneDDS errors `invalid data size` and `string data is not null-terminated` (`rmw_cyclonedds_cpp/src/serdata.cpp:384`) come from? `realsense2_camera_node` and `gripper_pose_estimator` print them, 41 to 135 times per launch; a message type that differs between Humble (Orin) and Jazzy (NUC) is suspected.
3. NUC and Orin: does the first bring-up on the builds of 2026-11-22 (Orin all `main` with FoundationPose; NUC `manipulation_stack` `main`) pass preflight, and does `gripper_pose_estimator` start and fit? Nothing has run on them yet.
4. Orin: should `unitree_interface` be pulled to `origin/main` (2 commits ahead of `599e2ff`; the NUC has `ffb78c3`), and `eigen_vector` `repos/orin.repos` moved with it?
5. NUC: `elevator_states` `f6261be` (elevator 1 places) is a local commit: should it go to GitHub, and which places file should `door_state_node` read for the next freight elevator run?
6. NUC and Orin: when `deploy.sh` stops copying `bringup` to `src/bringup` (the note under "Machines"), can `deploy.sh build` be used again?
7. Thor: should the stack move from `~/eigen_vector_ws_bringup` (copied `bringup`, `vlm` of 2026-10-29) to `~/eigen_vector_ws` with an `eigen_vector` checkout, as on the NUC and the Orin? Should it start on boot, and which `orchestrator` checkout should the Thor have for `vlm_server` (item 12)?

### Not in use

Other workspaces on the machines; nothing that runs comes from them (checked 2026-11-08).

- NUC: `eigen_vector_orch_ws`, `claude_build_ws`, `unitree_interface_ws` (checked 2026-11-22), `eigen_vector_ws_apt`, `eigen_vector_ws_lio`, `fsm_check_ws`, `nav_fix_ws`, `scratch_ekf_tf_ws`, `unitree_test_ws`
- Orin: `eigen_vector_scratch_ws`, `eigen_vector_ws_nav`, `ev_humble_ws`, `humble_ws`, `thor_aligned_ws`
- Thor: `eigen_vector_ws`, `scratch_pixi_ws`, `scratch_unitree_ws`

On the NUC, `goal_floor_test_ws` (only `route_planner`, built 2026-11-22 13:47) is a teammate's test workspace; check with the team before you source or delete it.

### Check it again

On each machine, for each workspace: the branch, the commit, and the local changes of every repository under `src/`.

```bash
for d in ~/eigen_vector_ws/src/*/; do
  git -C "$d" rev-parse --git-dir >/dev/null 2>&1 || { echo "$(basename $d) | not git"; continue; }
  echo "$(basename $d) | $(git -C $d rev-parse --abbrev-ref HEAD) @ $(git -C $d log -1 --format=%h) | changed $(git -C $d status --porcelain | wc -l)"
done
```

Then change the date under "How to read it", update the tables, remove the questions answered, and update the status of each item below.

## What the machines must change

The orchestrator on `main` expects some things from the NUC, the Orin and the Thor that are not in this repository: other repositories' branches, files deployed by hand, and processes started by hand. This part lists what has to change on those machines, and the contracts in this repository that check them, so the two stay in step.

- When an item is done on the robot, mark it **done** with the date and who did it. When it is no longer needed, delete it.
- Every item names the contract or tree that depends on it, so a check on the robot can be tied back to the code.

Status words: **open** (not done), **check** (may already be done; confirm on the machine), **done**. Each item also says whether the repository side (contracts, trees, preflight, and the other repositories on GitHub) is done; that never makes the robot side done.

Repository side checked against `ltx-reorg` (now in `main`) and GitHub on 2026-11-17. Robot side last seen on 2026-11-22 at 15:10 for the NUC and the Orin, on 2026-11-19 at 21:30 for the Thor (the snapshot above). The robot-side notes of 2026-11-19 below were not run again on the 2026-11-22 builds.

### NUC

#### 1. Stop depending on `/unitree_interface/ready_locomotion`. Status: done in this repository (2026-11-17, now in `main`)

- **Why:** preflight no longer stands the robot up (`852dcdc`), and the `ready_walk` skill that called this service is deleted. Nothing calls `ready_locomotion` any more.
- **Done in this repository:** the `ready_walk` contract is deleted; `nuc_up` no longer lists the service (it still checks `/unitree_interface/fsm_id` and `/unitree_interface/current_mode`, which show that `unitree_interface` runs); the fake robot no longer serves it.
- **On the NUC:** nothing to remove. `unitree_interface` may keep the service; no tree calls it. Never call it on a standing robot: it damps the robot, then stands it up again.

#### 2. `unitree_interface` must publish `/unitree_interface/fsm_id`. Status: done (2026-11-19)

- **Why:** `robot_ready` (preflight `robot_up`) reads it, and `nuc_up` waits for it. Without it every preflight stops with "I am not in ready locomotion".
- **On the NUC:** `unitree_interface` `main` @ `421e70f`. The topic gives `0` with the robot in the gantry and `801` standing in ready locomotion; preflight's `robot_ready` passed.

#### 3. `door_state_node` must start with the NUC stack. Status: done (2026-11-19)

- **Why:** `nuc_up` waits for `/door_state`, and `door_open` reads it. Preflight starts the NUC stack with `bringup nuc.launch.py` (`launch_nuc_stack`).
- **On the NUC:** `nuc.launch.py` includes `door_state_node.launch.py`; `/door_state` publishes at 5 to 10 Hz with no hand-started session.
- **With every map change:** `places_file` in `elevator_states` `config/elevator_door_state.yaml` on the NUC names the places file whose `cabin` and `hall` areas it uses ("Only on a machine"); set it to the map in use and build `elevator_states`.

#### 4. Run the NUC stack from `nuc.launch.py`, not the two split launch files. Status: done (2026-11-19)

- **Why:** the stale-node check in preflight (`no_stray_processes`) expects every stack node to come from `nuc.launch.py` (`HOSTS` in `orchestrator/orchestrator/health/stale_nodes.py`).
- **On the NUC:** the stack ran from one `bringup nuc.launch.py`, and preflight passed with no stale-node warning.

#### 5. Remove `floor_map_selector`. Status: done (2026-11-19)

- **Why:** `nuc_up` no longer waits for `/prior_map` (`8cbc9be`). Foxglove shows the current floor from `/route_planner/terrain`.
- **On the NUC:** `bringup` now builds from `src/eigen_vector` (`main` @ `3219b75` since 2026-11-22), which has no `floor_map_selector`; the hand-edited copy in `src/bringup` is gone.

#### 6. Stop the old EKF-reset watcher. Status: open

- **Why:** each look resets the body EKF itself with `reset_ekf` (`/body_ekf/reset`). A watcher started beside a tree resets the EKF a second time.
- **Repository side:** done; nothing in this repository starts the watcher.
- **Change on the NUC:** do not start `~/watch_records_20261108.sh`. Delete it and `~/ekf_reset.sh` (and `~/ekf_reset.sh.restart_bak`). Still there on 2026-11-19.

#### 7. `manipulation_stack` must provide what the press flow uses. Status: done, except the fingertip estimate (check)

The NUC runs `main` @ `96b2ca2` since 2026-11-22 (it holds all of `fix/button-press-aim` @ `e710393`, which ran on 2026-11-19), built as `deploy.sh` builds it. Seen on 2026-11-19, on `e710393`:

| Needed by | What | Seen |
|---|---|---|
| `check_reach` (every look) | service `/check_reach`, type `humanoid_manipulation_interfaces/srv/CheckReach`, from `ik_server`, started by `move_to_pose.launch.py` | present; every look of the run used it |
| `reset_ekf` (every look) | service `/body_ekf/reset` | present |
| `press_button` capture | static TF frames `right_ee` and `left_ee` on `*_wrist_yaw_link`, published by `move_to_pose` | present (`tf2_echo foot_world right_ee`) |
| `press_button` residual (the real fingertip) | TF frames `right_dex1_base_link_estimate` and `left_dex1_base_link_estimate`, the gripper's depth fit (`config.capture` in `g1_skills/g1_skills/press_button/contract.yaml`) | published while the gripper is in view, about once a second; until `8be696a` the capture used a fit only within 1 s of the deepest point, so 29 of 33 judged presses fell back to the encoders. Re-check question 1 |
| `set_press_offset` | `/move_to_pose_server/set_parameters` | present |

- If `/check_reach` is missing, every look fails and the robot asks a person to press each button.

#### 8. The places file must have a `floors` table. Status: done (2026-11-19)

- `fvd_map.places.yaml` has the `floors` table and the freight elevator's places. Since 2026-11-19 the site file (`orchestrator/config/sites/stanley.yaml`) names its copy `fvd_map_callpanel_tilt.places.yaml`, the same but for the floor 2 call panel place, re-recorded where the operator stood the robot after the press test (1.930, -0.902, yaw -14.1 deg; was 2.005, -1.036, -6.3 deg). `slam_elevatorlio` and `door_state_node` still read `fvd_map.places.yaml`, whose areas are the same.

#### 9. Clear old press references after a map or place change. Status: open, when needed

- The executor keeps every press judged pressed in `~/orch_state/press_references.jsonl` (it reads the old `~/.ros/orchestrator_press_references.jsonl` while only that exists, and copies it there on the first save; the old file stays) and uses it to suggest stances. The file never expires. After the map or a panel place moves, move the new file and the old one aside, or the old one comes back. On 2026-11-19 neither file existed.

#### 10. Small things the demo assumes. Status: open

- `launch_run_viewer` starts the viewer from this repository (`~/orchestrator_ws/src/orchestrator/run_viewer/`; repository side done, `9e8e87e`). The hand copy in `~/run_viewer/` is no longer used; move it aside. Still there on 2026-11-19.
- `~/fake_button_pose.py` stays disabled; delete it if nobody needs it. Still there on 2026-11-19.

#### 10b. Build the `g1_skills` package. Status: done (2026-11-19)

- **On the NUC:** the three packages are built from `36b2a70` (2026-11-19 21:06, `--symlink-install`; the checkout is now `main` @ `b5e8cca`, and a symlink install needs no rebuild for it); on `00a6297` the executor logged `45 skills, 13 places` at start.

### Orin

#### 11. `orin.launch.py` must start `gripper_pose_estimator`. Status: done, except its output (check)

- **Why:** `orin_up` waits for the estimator's node (`/gripper_estimation/get_parameters`), and `launch_orin_stack` no longer starts a second estimator in tmux. `press_button` takes the fingertip for its residual from the estimate (item 7).
- **On the Orin:** on 2026-11-19 `orin.launch.py` started it from `manipulation_stack` `fix/button-press-aim` @ `a4dac89`, and `/gripper_estimation/get_parameters` answered. Since 2026-11-22 the Orin has `manipulation_stack` `main` and `eigen_vector` `main` with FoundationPose; that build has not run yet (re-check question 3). On 2026-11-19 it published the estimate frames while the gripper is in view; how the press capture uses them is re-check question 1, the CycloneDDS errors question 2.

### Thor

#### 12. `vlm_server` on the Thor: no longer required. Status: open (robot side only)

- **Repository side:** done (`9ae7b2f`). `thor_up` checks only `/vlm_ground_server/ground_boxes`; it no longer requires `http://vector-thor:8090/v1/models`. No tree asks the VLM (only `ask_vlm` and the disabled `door_open_vlm` use it), so a Thor whose VLM is down no longer stops preflight.
- **Still on the robot:** the Thor's `~/orchestrator_ws` is `ltx-dev` @ `8d8fe7d`, which has no `orchestrator/orchestrator/vlm/server.py`, so `launch_thor_stack` cannot start `vlm_server` (in the session `vlm_server`, `--model edgellm` by default) until that checkout moves to `main` (`ltx-dev` is deleted on GitHub). No old `cosmos_vlm` session ran on 2026-11-19. Preflight passes either way.

### Laptop (operator)

#### 13. The agent bridge. Status: done (2026-11-19)

- The bridge's address is `agent_bridge` in `orchestrator/config/sites/stanley.yaml` (the laptop's Tailscale address, port 8771). The executor (launched with `site:=stanley`) and `agent_bridge.py --site stanley` both read it. For another laptop, change it there and rebuild on the NUC.
- Set up once as in [mission_control/README.md](../mission_control/README.md) (the venv with `claude-agent-sdk`); the operator laptop's venv was made on 2026-11-19.
- The bridge uses the laptop's Claude login. An `ANTHROPIC_API_KEY` (or another auth variable) in the bridge's shell takes precedence over that login; on 2026-11-19 an invalid one failed every agent call with "Invalid API key" (run 002 of session `20261119T214319Z-198c58`). Start the bridge with it unset: `env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN ~/.venvs/agent_bridge/bin/python mission_control/agent_bridge.py --site stanley`.
- Run the bridge from a checkout of the branch the NUC runs: a call with another `protocol.PROTOCOL` fails with both versions in its error. The bridge reads each agent skill's `spec.py` and `prompt.py` from `g1_skills/` in its checkout.
