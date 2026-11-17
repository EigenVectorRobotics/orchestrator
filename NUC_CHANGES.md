# Changes the robot computers need

The orchestrator on `ltx-reorg` expects some things from the NUC, the Orin and the Thor that are not in this repository: other repositories' branches, files deployed by hand, and processes started by hand. This file lists what has to change on those machines, and the contracts in this repository that check them, so the two stay in step.

- `docs/deployment.md` records what each machine runs, as last checked. This file records what has to change.
- When an item is done on the robot, mark it **done** with the date and who did it. When it is no longer needed, delete it.
- Every item names the contract or tree that depends on it, so a check on the robot can be tied back to the code.

Status words: **open** (not done), **check** (may already be done; confirm on the machine), **done**.

## NUC

### 1. Stop depending on `/unitree_interface/ready_locomotion`. Status: done in this repository (2026-11-17, `ltx-reorg`)

- **Why:** preflight no longer stands the robot up (`852dcdc`), and the `ready_walk` skill that called this service is deleted. Nothing calls `ready_locomotion` any more.
- **Done in this repository:** the `ready_walk` contract is deleted; `nuc_up.yaml` no longer lists the service (it still checks `/unitree_interface/fsm_id` and `/unitree_interface/current_mode`, which show that `unitree_interface` runs); `fake_robot.py` no longer serves it.
- **On the NUC:** nothing to remove. `unitree_interface` may keep the service; no tree calls it. Never call it on a standing robot: it damps the robot, then stands it up again.

### 2. `unitree_interface` must publish `/unitree_interface/fsm_id`. Status: check

- **Why:** `robot_ready` (preflight `robot_up`) reads it, and `nuc_up` waits for it. Without it every preflight stops with "I am not in ready locomotion".
- **Source:** branch `jean/fsm-id-topic` (`e234864` on the NUC when checked on 2026-11-08). This branch is not on `main`.
- **Check:** `ros2 topic echo --once /unitree_interface/fsm_id` gives `801` with the robot standing in ready locomotion.

### 3. `door_state_node` must start with the NUC stack. Status: check

- **Why:** `nuc_up` waits for `/door_state`, and `door_open` reads it. Preflight starts the NUC stack with `bringup nuc.launch.py` (`launch_nuc_stack.yaml`).
- **Problem:** the `nuc.launch.py` that includes `door_state_node.launch.py` is on the `eigen_vector` branch `jean/door-state-bringup`, not on `main`. The bringup redeployed on 2026-11-08 did not include it; `door_state_node` then ran by hand in the tmux session `door_state`.
- **Change:** deploy bringup with the `door_state_node` include (merge `jean/door-state-bringup`, or redeploy from it), so that `launch_nuc_stack` alone gives `/door_state`.
- **Check:** after `launch_nuc_stack`, `ros2 topic hz /door_state` shows messages without any hand-started session.

### 4. Run the NUC stack from `nuc.launch.py`, not the two split launch files. Status: check

- **Why:** the stale-node check in preflight (`no_stray_processes`) expects every stack node to come from `nuc.launch.py`. On 2026-11-08 the NUC ran `~/nuc_manip_only.launch.py` and `~/nuc_nav_only.launch.py` instead. Then every preflight warns about every node and pauses 4.5 s.
- **Change:** go back to the single `nuc.launch.py` (with item 3), or add the split launch files to the NUC's launch list in `orchestrator/orchestrator/stale_nodes.py` (`HOSTS`).

### 5. Remove `floor_map_selector`. Status: open

- **Why:** `nuc_up` no longer waits for `/prior_map` (`8cbc9be`). Foxglove shows the current floor from `/route_planner/terrain`.
- **Change on the NUC:** remove the hand-made `~/eigen_vector_ws/src/bringup/scripts/floor_map_selector.py`, its line in bringup's `CMakeLists.txt` and its node in `launch/nuc.launch.py`, at the next bringup deploy.

### 6. Stop the old EKF-reset watcher. Status: open

- **Why:** each look resets the body EKF itself with `reset_ekf` (`/body_ekf/reset`). A watcher started beside a tree resets the EKF a second time.
- **Change on the NUC:** do not start `~/watch_records_*.sh`. Delete it and `~/ekf_reset.sh` (and `~/ekf_reset.sh.restart_bak`).

### 7. `manipulation_stack` must provide what the press flow uses. Status: check

The NUC ran `cam-gripper-estimation` @ `ba0cbfa` with uncommitted changes when checked; `main` is the branch to deploy. Confirm that the deployed build has all of these:

| Needed by | What | Source |
|---|---|---|
| `check_reach` (every look) | service `/check_reach`, type `humanoid_manipulation_interfaces/srv/CheckReach`, from `ik_server`, started by `move_to_pose.launch.py` | branch `feat/ik-server` |
| `reset_ekf` (every look) | service `/body_ekf/reset` | `e63f2b8` or later (in `main`) |
| `press_button` capture | static TF frames `right_ee` and `left_ee` on `*_wrist_yaw_link`, published by `move_to_pose` | the version that stopped publishing `/right_ee_pose` and `/left_ee_pose` |
| `set_press_offset` | `/move_to_pose_server/set_parameters` | |

- **Check:** `ros2 service list | grep -E "check_reach|body_ekf/reset"` and `ros2 run tf2_ros tf2_echo foot_world right_ee`.
- If `/check_reach` is missing, every look fails and the robot asks a person to press each button.

### 8. The places file must have a `floors` table. Status: check

- **Why:** the planner fills `goal_floor` from the floor of the goal place (`planner.fill_floor_parameters`). Without a `floors` table in the places file nothing is filled, the tree keeps its default `goal_floor`, and a goal on the other floor can be refused.
- **File:** `~/maps/two_floor_clean_cabin2.places.yaml` on the NUC. It needs `floors: {"floor 1": {z_min, z_max}, "floor 2": {z_min, z_max}}`, and every place used by `freight_elevator_go_to_place` (`target_floor_1/2`, the call panels, the inside panels, the door-out places).
- **Always** start the orchestrator with `places_file:=/home/vector/maps/two_floor_clean_cabin2.places.yaml`. The launch default, `config/places.yaml`, holds only two placeholders and cannot run the elevator tree.
- `skill_registry/trees/freight_elevator_go_to_place.xml` names `~/maps/orchestrator/…`; every other document names `~/maps/…`. Confirm the real path and fix the tree comment.

### 9. Clear old press references after a map or place change. Status: open, when needed

- The executor keeps every press judged pressed in `~/.ros/orchestrator_press_references.jsonl` and uses it to suggest stances. The file never expires. After the map or a panel place moves, move the old file aside.

### 10. Small things the demo assumes. Status: check

- `~/bags/` exists (`docs/demo.md` step 3 records the raw bag there).
- `~/run_viewer/` is checked out (`launch_run_viewer` starts it).
- `~/fake_button_pose.py` stays disabled; delete it if nobody needs it.

## Orin

### 11. `orin.launch.py` must start `gripper_pose_estimator`. Status: check

- **Why:** `orin_up` waits for `/left_gripper_pose` and `/right_gripper_pose`, and `launch_orin_stack` no longer starts a second estimator in tmux.
- **When checked:** the Orin ran `manipulation_stack` `cam-gripper-estimation` @ `ba0cbfa` with 2 uncommitted files. Deploy `main`, which has the estimator (#10).

## Thor

### 12. Keep `cosmos_vlm_server` running, or stop requiring it. Status: open

- **Why:** `thor_up` requires `http://vector-thor:8090/v1/models` (`cosmos_vlm_server`) as well as `/vlm_ground_server/ground_boxes`, and preflight stops every task when `thor_up` fails. No tree uses the VLM any more; the grounding is what the press flow needs.
- **Choose one:** start `cosmos_vlm_server` in the session `cosmos_vlm` whenever the Thor stack is up (as `launch_thor_stack` does), or remove the `http` line from `skill_registry/contracts/thor_up.yaml`.
- `launch_thor_stack` starts `cosmos_vlm_server.py` from the Thor's `~/orchestrator_ws`, which is on an old branch deleted on GitHub (`docs/deployment.md`). Redeploy it from the current orchestrator branch, or drop it with this item.

## Laptop (operator)

### 13. The agent bridge. Status: check

- The executor connects to `100.107.208.25:8771` by default (`agent_push_host`, `agent_push_port`), the laptop's Tailscale address. Another laptop needs `agent_push_host:=<its Tailscale address>` at launch, and `agent_bridge.py --listen <address>`.
- Set up once as in `mission_control/README.md` (the venv with `claude-agent-sdk`).
