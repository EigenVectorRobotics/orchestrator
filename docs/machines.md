# Machines

The Stanley G1's computers in two parts: what runs where, as last checked on the machines (a snapshot), and what the NUC, the Orin, the Thor and the operator laptop must change for the orchestrator on `ltx-reorg`, with the contracts in this repository that check each change. How to bring the machines up and run a task is in [run.md](run.md).

## What runs where (snapshot)

What runs where: how to reach each machine, the workspace in use, the branch of each repository, and what exists only on a machine. This part is a snapshot. Check it again (the commands at the end of this part) after every deploy, and update the tables.

### How to read it

Three kinds of statement, marked as such:

- **Checked (2026-11-08, evening):** seen on the machine then. This is the only machine state in this part, and it has not been checked since. Everything not marked otherwise is of this kind.
- **To deploy:** the branch the team runs from now on. It is a plan, not machine state: the machine still had what the row says was checked.
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

### Repositories on each machine

Branch @ commit, and local changes, as checked on 2026-11-08. "No upstream": the local branch tracks no remote branch. A row marked **to deploy** names the branch the team runs from now on; what was checked is in its last column until the machine is updated.

**bringup comes from `eigen_vector` `main`**, directory `bringup/`. Since 2026-10-26 (`eigen_vector` `8e498d7`, "bringup lives here") the package lives in `eigen_vector`; `deploy.sh` sends it and `provision.sh` copies it to `src/bringup` on each machine, so it is never a git checkout there. The separate repository `EigenVectorRobotics/bringup` stopped at 2026-10-27 (`4c70a72`): its `nuc.launch.py` starts only the arm visual servoing (no robot interface, navigation, SLAM or `door_state_node`), so `nuc_up` cannot pass with it. Do not deploy from it.

#### NUC, `~/eigen_vector_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `bringup` | **to deploy:** `eigen_vector` `main`, `bringup/` (`dfabc71` on GitHub, 2026-11-17), copied by `deploy.sh` | checked: not a git checkout, edited by hand (see "Only on a machine") |
| `manipulation_stack` | **to deploy:** `main` (`4f12ae1` on 2026-11-08: the gripper estimation merged as #10; contains `e63f2b8`, body_ekf `~/reset`). **GitHub:** `main` is `f8d28b3`, with `ik_server` (#11, 2026-11-14), which `check_reach` needs | checked: `cam-gripper-estimation` @ `ba0cbfa` with 6 files changed, not committed (`g1_pilot/src/move_to_pose/{planning.cpp,server.cpp,server.h}`, `humanoid_arm_skills/humanoid_arm_skills/gripper_pose_estimator.py`, `humanoid_arm_skills/launch/humanoid_arm_visual_servoing.launch.py`, `humanoid_arm_skills/package.xml`) |
| `navigation_stack` | `jean/elevator-side` @ `39e6139` | no upstream; one untracked file, `slam_elevatorlio/slam_elevatorlio/slam_elevatorlio_parameters.py` |
| `elevator_states` | `jean/door-state-in-out` @ `8ad0552` | no upstream; clean |
| `unitree_interface` | `jean/fsm-id-topic` @ `e234864` | no upstream; clean. **GitHub:** merged into `main` (#3, `421e70f`, 2026-11-09), and the branch is deleted; `eigen_vector` `repos/nuc.repos` names `main` |
| `livox_ros_driver2` | `master` @ `7e4114a` | clean |
| `rmw_cyclonedds` | `jazzy` @ `2659b2b` | clean |

#### NUC, `~/orchestrator_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `orchestrator` | **to deploy:** `ltx-reorg` | checked: `jean/planner-tree-library` @ `e99f402` |

#### Orin, `~/eigen_vector_ws/src`

| Repository | Branch @ commit | Local state |
|---|---|---|
| `bringup` | from `eigen_vector` `main`, `bringup/`, copied by `deploy.sh`; not a git checkout (copied 2026-11-02) | |
| `manipulation_stack` | `cam-gripper-estimation` @ `ba0cbfa` | 2 files changed, not committed |
| `track_anything` | `perf/track3d-latest-frame` @ `191b222` | no upstream; 7 files changed, not committed |
| `vlm` | `main` @ `3ca4861` | clean |
| `unitree_interface` | `main` @ `5e3257a` | clean |
| `rmw_cyclonedds` | `humble` @ `e370e09` | clean |

#### Thor

| Workspace | Repository | Branch @ commit | Local state |
|---|---|---|---|
| `~/eigen_vector_ws_bringup` | `bringup` | from `eigen_vector` `main`, `bringup/`, copied by `deploy.sh`; not a git checkout | |
| | `vlm` | `main` @ `3ca4861` | clean; `ground_server` runs from here |
| | `rmw_cyclonedds` | `jazzy` @ `2659b2b` | clean |
| `~/orchestrator_ws` | `orchestrator` | **to deploy:** `ltx-reorg` | checked: `tingxual/vec-125-scope-the-agentic-reasoning-topic` @ `33af8fd` (2026-11-04), a branch now deleted on GitHub and in the team's clones. `launch_thor_stack` starts `orchestrator/orchestrator/vlm/server.py` from this checkout; since `thor_up` checks only the grounding, preflight does not need it |
| `~/eigen_vector_ws` | `track_anything` `trt` @ `f5a1f47`, `vlm` `thor` @ `3ca4861` | | not in use |

Running when checked (2026-11-08): on the Thor, the tmux session `eigen_vector_bringup` (`ground_server`); no `vlm_server` session, so the VLM endpoint `http://vector-thor:8090/v1` did not answer. The Thor rebooted on its own on 2026-11-08 at about 21:20 and came back with no stack ([troubleshooting.md](troubleshooting.md), "Machines and processes").

### Only on a machine (not in any repository)

| Machine | Path | What | Status |
|---|---|---|---|
| NUC | `~/eigen_vector_ws/src/bringup/config/cyclonedds.xml` | `MaxAutoParticipantIndex` 120, because CycloneDDS's automatic participant index range (32 by default) filled up and new processes failed with `Failed to find a free participant index` | **GitHub:** now on `eigen_vector` `main` (`ParticipantIndex` auto, `MaxAutoParticipantIndex` 120; PR #3 and `dfabc71`, 2026-11-14): a redeploy from `main` keeps it, no hand edit needed |
| NUC | `~/eigen_vector_ws/src/bringup/launch/nuc.launch.py` | includes `elevator_states`' `door_state_node.launch.py` | **GitHub:** on `eigen_vector` `main` since 2026-11-10 (PR #2 from `jean/door-state-bringup`, `77382fb`): a redeploy from `main` starts `door_state_node` with the NUC stack, so `nuc_up` gets `/door_state`. Its door regions are set for `two_floor_clean.pcd` (`elevator_states` `config/elevator_door_state.yaml`, as the launch file notes); check them against `two_floor_clean_cabin2`. On 2026-11-08 the NUC ran a bringup without it, and `door_state_node` ran in its own tmux session `door_state` ([run.md](run.md#6-bring-a-machine-up-by-hand)) |
| NUC | `~/nuc_manip_only.launch.py`, `~/nuc_nav_only.launch.py` | the NUC stack split in two: robot interface, arm control and foxglove (`tmux -L eigen_vector`); navigation and SLAM (`tmux -L nav`, log `~/nav_launch.log`) | what ran on 2026-11-08 evening; not in any repository |
| NUC | `~/maps/two_floor_clean_cabin2.pcd`, `~/maps/two_floor_clean_cabin2.places.yaml` (backups `*.bak_*`) | the map in use and its places and areas (`freight elevator cabin`, `freight elevator hall`) | the executor, `slam_elevatorlio` and `door_state_node` read the places file at start |
| NUC | `~/fake_button_pose.py` | fixed button poses for `press_button` with no Thor | disabled: do not start it |
| NUC | `~/watch_records_*.sh`, `~/ekf_reset.sh` | an old EKF reset outside the trees | not used: `ekf_hold` calls `reset_ekf` |
| NUC | `~/places_markers.py` | publishes the places as markers for Foxglove (`tmux -L places`) | optional; stops with a reboot |
| NUC | `~/orch_runs/` | run records: one folder per bring-up session, `runs/<n>-<tree>/` each | the operator clears old runs by hand |

### Expected after the `ltx-reorg` deploy (from the code, not checked)

Paths the orchestrator on `ltx-reorg` creates or uses on the NUC, read from the code. None of them was checked on a machine.

| Path | What |
|---|---|
| `~/orch_state/press_references.jsonl` | the presses judged pressed, for stance suggestions; the old `~/.ros/orchestrator_press_references.jsonl` is read while only it exists, and copied here on the first save (the old file stays) |
| `~/bags/` | the site's raw bag (`raw_bag` in `orchestrator/config/sites/stanley.yaml`), recorded by the launch |
| `~/orchestrator.log` | the orchestrator's log when started as in [run.md](run.md#3-start-the-orchestrator); the executor logs the agent bridge link there, and `start.sh` and `status.sh` read it |
| tmux `run_viewer` | the Run Viewer, started by preflight (`launch_run_viewer`) from `~/orchestrator_ws/src/orchestrator/run_viewer/`; a hand copy in `~/run_viewer/` is no longer used (item 10 below) |

### Re-check on the robot

Questions the next check must answer. Each is open until someone looks on the machine.

1. NUC and Thor: does `~/orchestrator_ws/src/orchestrator` run `ltx-reorg`, at which commit, with which local changes?
2. NUC: is `bringup` redeployed from `eigen_vector` `main`, so that `nuc.launch.py` starts `door_state_node` and `cyclonedds.xml` has `MaxAutoParticipantIndex` 120 with no hand edit? Is `floor_map_selector` gone with it?
3. NUC: does the stack run from one `nuc.launch.py`, or still from `~/nuc_manip_only.launch.py` and `~/nuc_nav_only.launch.py`?
4. NUC: which `manipulation_stack` is built? Is it `main` at `f8d28b3` or later, with `/check_reach` (`ik_server`) and `/body_ekf/reset`, and are the 6 uncommitted files on `cam-gripper-estimation` kept somewhere or dropped?
5. NUC: does `unitree_interface` run from `main` (with `/unitree_interface/fsm_id`) or still from `jean/fsm-id-topic`?
6. NUC: do `navigation_stack` (`jean/elevator-side`), `elevator_states` (`jean/door-state-in-out`) and their local changes still run, while `eigen_vector` `repos/nuc.repos` names `navigation_stack` `main` and `elevator_states` `feat/door_state`? Which is meant to run?
7. NUC: does `~/maps/two_floor_clean_cabin2.places.yaml` have the `floors` table, and is the places file at `~/maps/` (the site file's path)?
8. Orin: which `manipulation_stack` and `track_anything` are built, and does `orin.launch.py` start `gripper_pose_estimator`?
9. Thor: does the tmux session `eigen_vector_bringup` come up after a reboot, and is `vlm_server` started (`launch_thor_stack` starts it in the session `vlm_server`)?
10. Laptop: is the agent bridge address in `orchestrator/config/sites/stanley.yaml` (`agent_bridge`) the operator laptop's Tailscale address?

### Not in use

Other workspaces on the machines; nothing that runs comes from them.

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

Then change the date under "How to read it", move each **to deploy** row that is now on the machine into a plain row, remove the questions answered, and update the status of each item below.

## What the machines must change

The orchestrator on `ltx-reorg` expects some things from the NUC, the Orin and the Thor that are not in this repository: other repositories' branches, files deployed by hand, and processes started by hand. This part lists what has to change on those machines, and the contracts in this repository that check them, so the two stay in step.

- When an item is done on the robot, mark it **done** with the date and who did it. When it is no longer needed, delete it.
- Every item names the contract or tree that depends on it, so a check on the robot can be tied back to the code.

Status words: **open** (not done), **check** (may already be done; confirm on the machine), **done**. Each item also says whether the repository side (contracts, trees, preflight, and the other repositories on GitHub) is done; that never makes the robot side done.

Repository side checked against `ltx-reorg` and GitHub on 2026-11-17. Robot side last seen on 2026-11-08 (the snapshot above).

### NUC

#### 1. Stop depending on `/unitree_interface/ready_locomotion`. Status: done in this repository (2026-11-17, `ltx-reorg`)

- **Why:** preflight no longer stands the robot up (`852dcdc`), and the `ready_walk` skill that called this service is deleted. Nothing calls `ready_locomotion` any more.
- **Done in this repository:** the `ready_walk` contract is deleted; `nuc_up` no longer lists the service (it still checks `/unitree_interface/fsm_id` and `/unitree_interface/current_mode`, which show that `unitree_interface` runs); the fake robot no longer serves it.
- **On the NUC:** nothing to remove. `unitree_interface` may keep the service; no tree calls it. Never call it on a standing robot: it damps the robot, then stands it up again.

#### 2. `unitree_interface` must publish `/unitree_interface/fsm_id`. Status: check

- **Why:** `robot_ready` (preflight `robot_up`) reads it, and `nuc_up` waits for it. Without it every preflight stops with "I am not in ready locomotion".
- **Source:** `unitree_interface` `main`: `jean/fsm-id-topic` was merged as #3 (`421e70f`, 2026-11-09) and the branch is deleted. The NUC ran the branch head `e234864` when checked on 2026-11-08, which has the topic.
- **Repository side:** done. `robot_ready` and `nuc_up` read the topic; `eigen_vector` `repos/nuc.repos` names `unitree_interface` `main`.
- **Check:** `ros2 topic echo --once /unitree_interface/fsm_id` gives `801` with the robot standing in ready locomotion.

#### 3. `door_state_node` must start with the NUC stack. Status: check

- **Why:** `nuc_up` waits for `/door_state`, and `door_open` reads it. Preflight starts the NUC stack with `bringup nuc.launch.py` (`launch_nuc_stack`).
- **Repository side:** done. `jean/door-state-bringup` is merged into `eigen_vector` `main` (#2, `77382fb`, 2026-11-10): its `bringup/launch/nuc.launch.py` includes `door_state_node.launch.py`. The bringup redeployed on 2026-11-08 did not include it; `door_state_node` then ran by hand in the tmux session `door_state`.
- **Change on the NUC:** redeploy bringup from `eigen_vector` `main`, so that `launch_nuc_stack` alone gives `/door_state`. The door regions are set for `two_floor_clean.pcd`; check them against `two_floor_clean_cabin2` (the snapshot above).
- **Check:** after `launch_nuc_stack`, `ros2 topic hz /door_state` shows messages without any hand-started session.

#### 4. Run the NUC stack from `nuc.launch.py`, not the two split launch files. Status: check

- **Why:** the stale-node check in preflight (`no_stray_processes`) expects every stack node to come from `nuc.launch.py`. On 2026-11-08 the NUC ran `~/nuc_manip_only.launch.py` and `~/nuc_nav_only.launch.py` instead. Then every preflight warns about every node and pauses 4.5 s.
- **Repository side:** `launch_nuc_stack` starts `bringup nuc.launch.py`, and `HOSTS` in `orchestrator/orchestrator/health/stale_nodes.py` expects `nuc.launch.py` on the NUC and `orin.launch.py` on the Orin.
- **Change:** go back to the single `nuc.launch.py` (with item 3), or add the split launch files to the NUC's entry in `HOSTS`.

#### 5. Remove `floor_map_selector`. Status: open

- **Why:** `nuc_up` no longer waits for `/prior_map` (`8cbc9be`). Foxglove shows the current floor from `/route_planner/terrain`.
- **Repository side:** done. `eigen_vector` `main` has no `floor_map_selector`, so a bringup deploy from `main` replaces the hand-edited copy.
- **Change on the NUC:** remove the hand-made `~/eigen_vector_ws/src/bringup/scripts/floor_map_selector.py`, its line in bringup's `CMakeLists.txt` and its node in `launch/nuc.launch.py`, at the next bringup deploy.

#### 6. Stop the old EKF-reset watcher. Status: open

- **Why:** each look resets the body EKF itself with `reset_ekf` (`/body_ekf/reset`). A watcher started beside a tree resets the EKF a second time.
- **Repository side:** done; nothing in this repository starts the watcher.
- **Change on the NUC:** do not start `~/watch_records_*.sh`. Delete it and `~/ekf_reset.sh` (and `~/ekf_reset.sh.restart_bak`).

#### 7. `manipulation_stack` must provide what the press flow uses. Status: check

The NUC ran `cam-gripper-estimation` @ `ba0cbfa` with uncommitted changes when checked; `main` is the branch to deploy. On GitHub, `main` (`f8d28b3`, 2026-11-14) has `ik_server` (#11) and `e63f2b8`. Confirm that the deployed build has all of these:

| Needed by | What | Source |
|---|---|---|
| `check_reach` (every look) | service `/check_reach`, type `humanoid_manipulation_interfaces/srv/CheckReach`, from `ik_server`, started by `move_to_pose.launch.py` | `main` since #11 (`f8d28b3`); was branch `feat/ik-server` |
| `reset_ekf` (every look) | service `/body_ekf/reset` | `e63f2b8` or later (in `main`) |
| `press_button` capture | static TF frames `right_ee` and `left_ee` on `*_wrist_yaw_link`, published by `move_to_pose` | the version that stopped publishing `/right_ee_pose` and `/left_ee_pose` |
| `press_button` residual (the real fingertip) | TF frames `right_dex1_base_link_estimate` and `left_dex1_base_link_estimate`, the gripper's depth fit (`config.capture` in `g1_skills/g1_skills/press_button/contract.yaml`) | `gripper_pose_estimator` on the Orin (item 11); without them the residual uses the encoders. Not yet tested on the robot |
| `set_press_offset` | `/move_to_pose_server/set_parameters` | |

- **Check:** `ros2 service list | grep -E "check_reach|body_ekf/reset"` and `ros2 run tf2_ros tf2_echo foot_world right_ee`.
- If `/check_reach` is missing, every look fails and the robot asks a person to press each button.

#### 8. The places file must have a `floors` table. Status: check

- **Why:** the planner fills `goal_floor` from the floor of the goal place (`fill_floor_parameters` in `orchestrator/orchestrator/planning/planner.py`). Without a `floors` table in the places file nothing is filled, the tree keeps its default `goal_floor`, and a goal on the other floor can be refused.
- **File:** `~/maps/two_floor_clean_cabin2.places.yaml` on the NUC. It needs `floors: {"floor 1": {z_min, z_max}, "floor 2": {z_min, z_max}}`, and every place used by `freight_elevator_go_to_place` (`target_floor_1/2`, the call panels, the inside panels, the door-out places).
- Start the orchestrator with `site:=stanley`: the site file names this places file. Without a site the launch takes `orchestrator/config/places.yaml`, the placeholder used without a site, which cannot run the elevator tree.
- **Repository side:** the planner fills `goal_floor` when the table is there; `orchestrator/config/sites/stanley.yaml` names `/home/vector/maps/two_floor_clean_cabin2.places.yaml`.

#### 9. Clear old press references after a map or place change. Status: open, when needed

- The executor keeps every press judged pressed in `~/orch_state/press_references.jsonl` (it reads the old `~/.ros/orchestrator_press_references.jsonl` while only that exists, and copies it there on the first save; the old file stays) and uses it to suggest stances. The file never expires. After the map or a panel place moves, move the new file and the old one aside, or the old one comes back.

#### 10. Small things the demo assumes. Status: check

- `launch_run_viewer` starts the viewer from this repository (`~/orchestrator_ws/src/orchestrator/run_viewer/`; repository side done, `9e8e87e`). The hand copy in `~/run_viewer/` is no longer used; move it aside once the new one runs.
- `~/fake_button_pose.py` stays disabled; delete it if nobody needs it.

#### 10b. Build the `g1_skills` package. Status: open

- **Why:** the skills are the package `g1_skills`, one folder per skill (contract and code). Every node reads the contracts from it, and the executor loads the skills' code from it.
- **Change:** build the three packages, one worker, as in [run.md](run.md#2-deploy-on-the-nuc).
- **Check:** the executor logs `<n> skills, <m> places` at start, `<n>` the number of skill folders in `g1_skills/g1_skills/`, and `ros2 run skill_registry validate_tree <tree>` finds the contracts.

### Orin

#### 11. `orin.launch.py` must start `gripper_pose_estimator`. Status: check

- **Why:** `orin_up` waits for the estimator's node (`/gripper_estimation/get_parameters`), and `launch_orin_stack` no longer starts a second estimator in tmux. `press_button` takes the fingertip for its residual from the estimate (item 7).
- **Repository side:** done. `eigen_vector` `main` `bringup/launch/orin.launch.py` starts `humanoid_arm_skills` `gripper_pose_estimator`, and `manipulation_stack` `main` has it (#10).
- **When checked:** the Orin ran `manipulation_stack` `cam-gripper-estimation` @ `ba0cbfa` with 2 uncommitted files. Deploy `main` and bringup from `eigen_vector` `main`.

### Thor

#### 12. `vlm_server` on the Thor: no longer required. Status: open (robot side only)

- **Repository side:** done (`9ae7b2f`). `thor_up` checks only `/vlm_ground_server/ground_boxes`; it no longer requires `http://vector-thor:8090/v1/models`. No tree asks the VLM (only `ask_vlm` and the disabled `door_open_vlm` use it), so a Thor whose VLM is down no longer stops preflight.
- **Still on the robot:** the Thor's `~/orchestrator_ws` is on an old branch deleted on GitHub (the snapshot above), and runs the server as `cosmos_vlm_server.py` in the session `cosmos_vlm`. `launch_thor_stack` now starts `orchestrator/orchestrator/vlm/server.py` (model-agnostic, `--model edgellm` by default) in the session `vlm_server`. After a redeploy, stop the old `cosmos_vlm` session first, or the new server cannot take port 8090. Preflight passes either way.

### Laptop (operator)

#### 13. The agent bridge. Status: check

- The bridge's address is `agent_bridge` in `orchestrator/config/sites/stanley.yaml` (the laptop's Tailscale address, port 8771). The executor (launched with `site:=stanley`) and `agent_bridge.py --site stanley` both read it. For another laptop, change it there and rebuild on the NUC.
- `~/bags/` is created by the launch when the site records the raw bag.
- Set up once as in [mission_control/README.md](../mission_control/README.md) (the venv with `claude-agent-sdk`).
- Run the bridge from a checkout of the branch the NUC runs: a call with another `protocol.PROTOCOL` fails with both versions in its error. The bridge reads each agent skill's `spec.py` and `prompt.py` from `g1_skills/` in its checkout.
