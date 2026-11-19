# Machines

The Stanley G1's computers in two parts: what runs where, as last checked on the machines (a snapshot), and what the NUC, the Orin, the Thor and the operator laptop must change for the orchestrator on `integration`, with the contracts in this repository that check each change. How to bring the machines up and run a task is in [run.md](run.md).

## What runs where (snapshot)

What runs where: how to reach each machine, the workspace in use, the branch of each repository, and what exists only on a machine. This part is a snapshot. Check it again (the commands at the end of this part) after every deploy, and update the tables.

### How to read it

Three kinds of statement, marked as such:

- **Checked (2026-11-19, afternoon):** seen on the machine then, during the first robot run of `integration`. This is the only machine state in this part. Everything not marked otherwise is of this kind.
- **Checked (2026-11-08):** seen then and not looked at since; marked where it appears.
- **GitHub (2026-11-17):** read from the repositories on GitHub, not from a machine.

What to check on the robot is in "Re-check on the robot", as questions.

### Machines

| Machine | Runs | Reach it | OS, ROS | Workspace in use |
|---|---|---|---|---|
| NUC | robot interface, arm control, body EKF, navigation, `door_state_node`, the orchestrator | `ssh vector@vector-nuc` (tailscale); `192.168.123.5` on the robot wire | Ubuntu 24.04, Jazzy | `~/eigen_vector_ws` (stack), `~/orchestrator_ws` (orchestrator, an overlay on it) |
| Orin | camera (`realsense2_camera`, apt), `seed_bbox`, `track_3d`, gripper pose estimator | from the NUC: `ssh unitree@192.168.123.164` (the NUC has the key) | Ubuntu 22.04, Humble | `~/eigen_vector_ws` |
| Thor | Cosmos daemon (docker container `edgellm`), `ground_server`, `vlm_server` | from the NUC: `ssh vector@192.168.123.1` (the NUC has the key); `vector-thor` over tailscale | Ubuntu 24.04, Jazzy | `~/eigen_vector_ws_bringup` (stack), `~/orchestrator_ws` (only for `vlm_server`) |

- `env.sh` in each workspace is written by `eigen_vector/scripts/deploy.sh`; source it before any ROS command on that machine. On the NUC, `~/orchestrator_ws/env.sh` sources the stack's environment too.
- Passwords are not kept in the repository: ask the team. From the laptop, the Orin and the Thor are reached most reliably through the NUC (`ssh vector@vector-nuc "ssh unitree@192.168.123.164 '...'"`); `ssh -J` to them has timed out.
- `deploy.sh launch orin` reaches the Orin at `192.168.123.164` through the NUC, so the laptop needs that address's current host key in `known_hosts`.
- A build by hand uses the arguments `deploy.sh` uses: `COLCON_ARGS` in `eigen_vector` `config/nuc.env` and `config/orin.env` (`--symlink-install` and `CycloneDDS_DIR`). On the NUC, one worker and never with the robot standing ([run.md](run.md#2-deploy-on-the-nuc)).
- `git fetch` on the NUC and the Orin needs your SSH agent: `ssh -A` to the NUC, and `ssh -A` again from there to the Orin.

### Repositories on each machine

Branch @ commit, and local changes, as checked on 2026-11-19. A stash named in a row holds the local changes the machine had before the branch in the row was checked out; `git stash list` in that checkout shows it.

**bringup comes from `eigen_vector` `main`**, directory `bringup/`. Since 2026-10-26 (`eigen_vector` `8e498d7`, "bringup lives here") the package lives in `eigen_vector`; `deploy.sh` sends it and `provision.sh` copies it to `src/bringup` on each machine, so it is never a git checkout there. The separate repository `EigenVectorRobotics/bringup` stopped at 2026-10-27 (`4c70a72`): its `nuc.launch.py` starts only the arm visual servoing (no robot interface, navigation, SLAM or `door_state_node`), so `nuc_up` cannot pass with it. Do not deploy from it.

#### NUC, `~/eigen_vector_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `bringup` | not a git checkout | `nuc.launch.py` includes `door_state_node.launch.py`; `cyclonedds.xml` has `MaxAutoParticipantIndex` 120; `floor_map_selector` is still in `nuc.launch.py` and `CMakeLists.txt` (item 5), so this is not a plain copy of `eigen_vector` `main` |
| `manipulation_stack` | `fix/button-press-aim` @ `826c8d7` (since 2026-11-19; the team's choice for the press) | clean. Stash "nuc: fix/final-leg-foot-world worktree ... before fix/button-press-aim 2026-11-19": the earlier `fix/final-leg-foot-world` @ `f8d28b3` worktree, of which only a `planning.cpp` change and a D435 mount in the URDF (x 0.068963) were not on the branch. The D435 mount now is the branch's (0.081099, 0.023467, 0.414921) |
| `navigation_stack` | `main` @ `91eef10` | 33 files changed, not committed (not looked at) |
| `elevator_states` | `main` @ `d71ad3d` | 1 file changed, not committed: `config/elevator_door_state.yaml` `places_file` is `/home/vector/maps/fvd_map.places.yaml` (see "Only on a machine") |
| `unitree_interface` | `main` @ `421e70f` | clean |
| `livox_ros_driver2` | `master` @ `7e4114a` | clean |
| `rmw_cyclonedds` | `jazzy` @ `2659b2b` | clean |

#### NUC, `~/orchestrator_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `orchestrator` | `integration` @ `00a6297` | clean; built 2026-11-19 |

#### Orin, `~/eigen_vector_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `bringup` | not a git checkout; `orin.launch.py` starts `gripper_pose_estimator` | |
| `manipulation_stack` | `fix/button-press-aim` @ `826c8d7` (since 2026-11-19) | clean. Stash "orin: main worktree ... before fix/button-press-aim 2026-11-19": `main` @ `4f12ae1` with 3 files changed (`gripper_fit.py`, `gripper_pose_estimator.py`, `utils.py`). Built: `g1_description`, `humanoid_manipulation_interfaces`, `humanoid_arm_skills`. Not built: `g1_pilot`, which needs `casadi`, not installed on the Orin; the Orin runs no `g1_pilot` node. `eigen_vector` `repos/orin.repos` does not list `manipulation_stack` |
| `track_anything` | `perf/track3d-latest-frame` @ `191b222` | 7 files changed, not committed |
| `vlm` | `main` @ `3ca4861` | clean |
| `unitree_interface` | `main` @ `5e3257a` | clean |
| `rmw_cyclonedds` | `humble` @ `e370e09` | clean |

#### Thor

| Workspace | Repository | Branch @ commit | Local state |
|---|---|---|---|
| `~/eigen_vector_ws_bringup` | `bringup` | not a git checkout | |
| | `vlm` | `main` @ `3ca4861` | clean; `ground_server` runs from here |
| | `rmw_cyclonedds` | `jazzy` @ `2659b2b` | clean |
| `~/orchestrator_ws` | `orchestrator` | `ltx-dev` @ `8d8fe7d` | has no `orchestrator/orchestrator/vlm/server.py`, so `launch_thor_stack` cannot start `vlm_server` from it (item 12) |
| `~/eigen_vector_ws` | `track_anything` `trt` @ `f5a1f47`, `vlm` `thor` @ `3ca4861` (checked 2026-11-08) | | not in use |

Running when checked (2026-11-19): the Thor had booted at 15:34 and ran no stack: no `eigen_vector_bringup` session, no `vlm_server` or `cosmos_vlm` session; the container `edgellm` came up with the bringup started by hand at 16:28. The VLM endpoint `http://vector-thor:8090/v1` was not started. The Thor also came back with no stack after it rebooted on its own on 2026-11-08 ([troubleshooting.md](troubleshooting.md), "Machines and processes").

### Only on a machine (not in any repository)

| Machine | Path | What | Status |
|---|---|---|---|
| NUC | `~/eigen_vector_ws/src/bringup/config/cyclonedds.xml` | `MaxAutoParticipantIndex` 120, because CycloneDDS's automatic participant index range (32 by default) filled up and new processes failed with `Failed to find a free participant index` | present (2026-11-19). **GitHub:** also on `eigen_vector` `main` (PR #3 and `dfabc71`, 2026-11-14), so a redeploy from `main` keeps it |
| NUC | `~/eigen_vector_ws/src/bringup/launch/nuc.launch.py` | includes `elevator_states`' `door_state_node.launch.py`, and still the hand-made `floor_map_selector` | `door_state_node` starts with the stack (2026-11-19). **GitHub:** the include is on `eigen_vector` `main` since 2026-11-10 (PR #2, `77382fb`); `floor_map_selector` is not (item 5) |
| NUC | `~/eigen_vector_ws/src/elevator_states/config/elevator_door_state.yaml` | `places_file: /home/vector/maps/fvd_map.places.yaml`, the door areas (`freight elevator cabin`, `freight elevator hall`) of the map in use. Committed on `elevator_states` `main` @ `d71ad3d`: `two_floor_clean_cabin2.places.yaml`; the refactored `elevator_states` has no such file, so the value cannot be committed there | set and built 2026-11-19. Change it with the map ([The map](run.md#the-map)), then build `elevator_states` |
| NUC | `~/nuc_manip_only.launch.py`, `~/nuc_nav_only.launch.py` | the NUC stack split in two (what ran on 2026-11-08) | not used on 2026-11-19: the stack ran from one `nuc.launch.py` |
| NUC | `~/maps/<map>.pcd`, `~/maps/<map>.places.yaml` (backups `*.bak_*`) | the maps and their places files; the one in use: [The map](run.md#the-map). The freight elevator is `fvd_map` (its places come from `two_floor_clean_cabin2`); `fvd_map_clean_elevator1` and `fvd_map_clean_elevator2` are two other elevators | the executor, `slam_elevatorlio` and `door_state_node` read the places file at start |
| NUC | `~/fake_button_pose.py` | fixed button poses for `press_button` with no Thor | still there (2026-11-19), disabled: do not start it |
| NUC | `~/watch_records_20261108.sh`, `~/ekf_reset.sh` | an old EKF reset outside the trees | still there (2026-11-19), not used: `ekf_hold` calls `reset_ekf` (item 6) |
| NUC | `~/run_viewer/` | the old hand copy of the Run Viewer | still there (2026-11-19); preflight starts the viewer from this repository (item 10) |
| NUC | `~/places_markers.py` | publishes the places as markers for Foxglove (`tmux -L places`) | optional; stops with a reboot |
| NUC | `~/orch_runs/` | run records: one folder per bring-up session, `runs/<n>-<tree>/` each | the operator clears old runs by hand |

### After the `integration` deploy

Paths the orchestrator on `integration` creates or uses on the NUC.

| Path | What | Seen (2026-11-19) |
|---|---|---|
| `~/orch_state/press_references.jsonl` | the presses judged pressed, for stance suggestions; the old `~/.ros/orchestrator_press_references.jsonl` is read while only it exists, and copied here on the first save (the old file stays) | neither file exists yet: no press has been judged pressed |
| `~/bags/` | the site's raw bag (`raw_bag` in `orchestrator/config/sites/stanley.yaml`), recorded by the launch | yes, `raw_<date>_<time>/`; it closes only when the orchestrator stops with Ctrl-C |
| `~/orchestrator.log` | the orchestrator's log when started as in [run.md](run.md#3-start-the-orchestrator); the executor logs the agent bridge link there, and `start.sh` and `status.sh` read it | yes |
| tmux `run_viewer` | the Run Viewer, started by preflight (`launch_run_viewer`) from `~/orchestrator_ws/src/orchestrator/run_viewer/` | not checked |

### Re-check on the robot

Questions the next check must answer. Each is open until someone looks on the machine.

1. Orin: with the gripper in the camera's view, does `gripper_pose_estimator` publish `right_dex1_base_link_estimate` and `left_dex1_base_link_estimate`, and does a press record `residual_from` other than `encoders`? On 2026-11-19 neither frame was seen (the arm was out of view), and every press used the encoders.
2. Orin: where do the CycloneDDS errors `invalid data size` and `string data is not null-terminated` (`rmw_cyclonedds_cpp/src/serdata.cpp:384`) come from? `realsense2_camera_node` and `gripper_pose_estimator` print them, 41 to 135 times per launch; a message type that differs between Humble (Orin) and Jazzy (NUC) is suspected.
3. NUC: what are the 33 uncommitted files in `navigation_stack`, and is `main` with them what is meant to run?
4. NUC: should `bringup` be redeployed from `eigen_vector` `main`, which removes `floor_map_selector` (item 5)?
5. Thor: should `eigen_vector_bringup` start on boot, and which `orchestrator` checkout should the Thor have for `vlm_server` (item 12)?

### Not in use

Other workspaces on the machines; nothing that runs comes from them (checked 2026-11-08).

- NUC: `eigen_vector_orch_ws`, `eigen_vector_ws_apt`, `eigen_vector_ws_lio`, `fsm_check_ws`, `nav_fix_ws`, `scratch_ekf_tf_ws`, `unitree_test_ws`
- Orin: `eigen_vector_scratch_ws`, `eigen_vector_ws_nav`, `ev_humble_ws`, `humble_ws`, `thor_aligned_ws`
- Thor: `eigen_vector_ws`, `scratch_pixi_ws`, `scratch_unitree_ws`

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

The orchestrator on `integration` expects some things from the NUC, the Orin and the Thor that are not in this repository: other repositories' branches, files deployed by hand, and processes started by hand. This part lists what has to change on those machines, and the contracts in this repository that check them, so the two stay in step.

- When an item is done on the robot, mark it **done** with the date and who did it. When it is no longer needed, delete it.
- Every item names the contract or tree that depends on it, so a check on the robot can be tied back to the code.

Status words: **open** (not done), **check** (may already be done; confirm on the machine), **done**. Each item also says whether the repository side (contracts, trees, preflight, and the other repositories on GitHub) is done; that never makes the robot side done.

Repository side checked against `ltx-reorg` and GitHub on 2026-11-17. Robot side last seen on 2026-11-19 (the snapshot above), by a Claude Code session operating the first `integration` run.

### NUC

#### 1. Stop depending on `/unitree_interface/ready_locomotion`. Status: done in this repository (2026-11-17, `ltx-reorg`)

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

#### 5. Remove `floor_map_selector`. Status: open

- **Why:** `nuc_up` no longer waits for `/prior_map` (`8cbc9be`). Foxglove shows the current floor from `/route_planner/terrain`.
- **Repository side:** done. `eigen_vector` `main` has no `floor_map_selector`, so a bringup deploy from `main` replaces the hand-edited copy.
- **Change on the NUC:** remove the hand-made `~/eigen_vector_ws/src/bringup/scripts/floor_map_selector.py`, its line in bringup's `CMakeLists.txt` and its node in `launch/nuc.launch.py`, at the next bringup deploy. Still there on 2026-11-19.

#### 6. Stop the old EKF-reset watcher. Status: open

- **Why:** each look resets the body EKF itself with `reset_ekf` (`/body_ekf/reset`). A watcher started beside a tree resets the EKF a second time.
- **Repository side:** done; nothing in this repository starts the watcher.
- **Change on the NUC:** do not start `~/watch_records_20261108.sh`. Delete it and `~/ekf_reset.sh` (and `~/ekf_reset.sh.restart_bak`). Still there on 2026-11-19.

#### 7. `manipulation_stack` must provide what the press flow uses. Status: done, except the fingertip estimate (check)

The NUC runs `fix/button-press-aim` @ `826c8d7` since 2026-11-19, built as `deploy.sh` builds it. Seen on 2026-11-19:

| Needed by | What | Seen |
|---|---|---|
| `check_reach` (every look) | service `/check_reach`, type `humanoid_manipulation_interfaces/srv/CheckReach`, from `ik_server`, started by `move_to_pose.launch.py` | present; every look of the run used it |
| `reset_ekf` (every look) | service `/body_ekf/reset` | present |
| `press_button` capture | static TF frames `right_ee` and `left_ee` on `*_wrist_yaw_link`, published by `move_to_pose` | present (`tf2_echo foot_world right_ee`) |
| `press_button` residual (the real fingertip) | TF frames `right_dex1_base_link_estimate` and `left_dex1_base_link_estimate`, the gripper's depth fit (`config.capture` in `g1_skills/g1_skills/press_button/contract.yaml`) | **not seen**: every press of 2026-11-19 recorded `residual_from: "encoders"` (session `20261119T214319Z-198c58`, runs 002 and 003, on the earlier branch). Re-check question 1 |
| `set_press_offset` | `/move_to_pose_server/set_parameters` | present |

- If `/check_reach` is missing, every look fails and the robot asks a person to press each button.

#### 8. The places file must have a `floors` table. Status: done (2026-11-19)

- `fvd_map.places.yaml` has the `floors` table and the freight elevator's places; the site file names it (`orchestrator/config/sites/stanley.yaml`).

#### 9. Clear old press references after a map or place change. Status: open, when needed

- The executor keeps every press judged pressed in `~/orch_state/press_references.jsonl` (it reads the old `~/.ros/orchestrator_press_references.jsonl` while only that exists, and copies it there on the first save; the old file stays) and uses it to suggest stances. The file never expires. After the map or a panel place moves, move the new file and the old one aside, or the old one comes back. On 2026-11-19 neither file existed.

#### 10. Small things the demo assumes. Status: open

- `launch_run_viewer` starts the viewer from this repository (`~/orchestrator_ws/src/orchestrator/run_viewer/`; repository side done, `9e8e87e`). The hand copy in `~/run_viewer/` is no longer used; move it aside. Still there on 2026-11-19.
- `~/fake_button_pose.py` stays disabled; delete it if nobody needs it. Still there on 2026-11-19.

#### 10b. Build the `g1_skills` package. Status: done (2026-11-19)

- **On the NUC:** the three packages are built from `integration` @ `00a6297`; the executor logs `45 skills, 13 places` at start.

### Orin

#### 11. `orin.launch.py` must start `gripper_pose_estimator`. Status: done, except its output (check)

- **Why:** `orin_up` waits for the estimator's node (`/gripper_estimation/get_parameters`), and `launch_orin_stack` no longer starts a second estimator in tmux. `press_button` takes the fingertip for its residual from the estimate (item 7).
- **On the Orin:** `orin.launch.py` starts it from `manipulation_stack` `fix/button-press-aim` @ `826c8d7`, and `/gripper_estimation/get_parameters` answers. Whether it publishes the estimate frames is re-check questions 1 and 2.

### Thor

#### 12. `vlm_server` on the Thor: no longer required. Status: open (robot side only)

- **Repository side:** done (`9ae7b2f`). `thor_up` checks only `/vlm_ground_server/ground_boxes`; it no longer requires `http://vector-thor:8090/v1/models`. No tree asks the VLM (only `ask_vlm` and the disabled `door_open_vlm` use it), so a Thor whose VLM is down no longer stops preflight.
- **Still on the robot:** the Thor's `~/orchestrator_ws` is `ltx-dev` @ `8d8fe7d`, which has no `orchestrator/orchestrator/vlm/server.py`, so `launch_thor_stack` cannot start `vlm_server` (in the session `vlm_server`, `--model edgellm` by default) until that checkout is updated. No old `cosmos_vlm` session ran on 2026-11-19. Preflight passes either way.

### Laptop (operator)

#### 13. The agent bridge. Status: done (2026-11-19)

- The bridge's address is `agent_bridge` in `orchestrator/config/sites/stanley.yaml` (the laptop's Tailscale address, port 8771). The executor (launched with `site:=stanley`) and `agent_bridge.py --site stanley` both read it. For another laptop, change it there and rebuild on the NUC.
- Set up once as in [mission_control/README.md](../mission_control/README.md) (the venv with `claude-agent-sdk`); the operator laptop's venv was made on 2026-11-19.
- The bridge uses the laptop's Claude login. An `ANTHROPIC_API_KEY` (or another auth variable) in the bridge's shell takes precedence over that login; on 2026-11-19 an invalid one failed every agent call with "Invalid API key" (run 002 of session `20261119T214319Z-198c58`). Start the bridge with it unset: `env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN ~/.venvs/agent_bridge/bin/python mission_control/agent_bridge.py --site stanley`.
- Run the bridge from a checkout of the branch the NUC runs: a call with another `protocol.PROTOCOL` fails with both versions in its error. The bridge reads each agent skill's `spec.py` and `prompt.py` from `g1_skills/` in its checkout.
