# Agent skills

An agent skill hands one decision of a tree to a model or to the operator. There are three: `press_judge`, `manipulation_recovery` and `operator_confirm`. Each contract (`skill_registry/contracts/<name>.yaml`) has `transport: agent`; the leaf is `AgentLeaf` (`orchestrator/orchestrator/skill_leaf.py`) and the runner is `Agent` (`orchestrator/orchestrator/agent.py`).

The agent bridge on the operator laptop (`mission_control/agent_bridge.py`) answers every call. For `press_judge` and `manipulation_recovery` it asks a Claude model that has no tools and reads no documents: it sees only the system prompt and the user message built in `mission_control/agent_prompts.py`. When this document and the prompt disagree, the prompt is what the model follows; when either disagrees with the code, the code is right. What the executor and the bridge share (phases, decisions, decision fields, the answer schema, the limits, the link framing) is written once, in `orchestrator/orchestrator/agent_spec.py`.

## The skills at a glance

| Skill | Phase | The tree calls it | Answered by | Decisions | Contract timeout |
|---|---|---|---|---|---|
| `press_judge` | `judge` | Right after every `press_button` that succeeded | the model, `JUDGE_SYSTEM` | `pressed`, `not_pressed` | 600 s |
| `manipulation_recovery` | `stance_check` | A look before the first press failed (`button_in_view`, `check_reach`, or any other step of the look) | the model, `recovery_system(limits)` | `continue`, `reposition`, `abort` | 600 s |
| `manipulation_recovery` | `diagnose` | `press_button` failed, `press_judge` said `not_pressed`, or another step of a press failed | the model, `recovery_system(limits)` | `reposition`, `switch_arm`, `offset_retry`, `retry`, `ask_help`, `abort` | 600 s |
| `operator_confirm` | `confirm` | No tree on this branch calls it; the tests do (`orchestrator/test/test_agent_push.py`, `mission_control/test/test_agent_bridge.py`) | the operator, in the bridge's terminal | `confirmed`, `not_confirmed` | 120 s |

`PHASE_DECISIONS` in `agent_spec.py` is the source for the decisions per phase. A decision in `GOING_ON` makes the leaf succeed; `not_pressed`, `not_confirmed`, `ask_help` and `abort` make it fail, and `abort` also fails the whole tree at once with the reason (`context.abort`).

## The press slot

Each panel runs the press slot `press_panel_button` (`skill_registry/trees/subtrees/press.xml`). The elevator subtrees (`subtrees/elevator.xml`, `call_elevator` and `press_floor_button`) set the panel first:

| Panel | `press_panel` | `button` | `press_base_offset_{x,y,z}` | `press_arm_min_y` | `press_ask` |
|---|---|---|---|---|---|
| Call panel | the floor's call-panel place | `down button` on both floors | 0, 0, 0 | 0.0 m | "Please press the call button for me." |
| Inside panel | the floor's inside-panel place | `floor 1` or `floor 2` | 0.003 m on each axis | 0.10 m | "Please press my floor button for me." |

On the floor 1 call panel `up button` grounded the light above the button, so the trees ask for `down button`. Outside, the arm on the button's side reached it; inside, the left arm failed IK for floor buttons at y +0.05 to +0.07 m in `pelvis` and the right reached them; hence `press_arm_min_y` 0.0 at the call panel and 0.10 inside.

1. **Base offset.** `set_press_offset` with the panel's base offset, so `check_reach` checks the goal the first press aims at. An offset never carries over from one panel to the other.
2. **Stance check** (`stance_check` subtree), up to 4 rounds. Each round runs `look`: walk to `recovery_goal` (the panel place at first; first to `recovery_approach` when `recovery_walk_in` is `yes`), wait 2.5 s for the robot to settle, then `look_here`. `look_here` runs, once per stance, `prepare_arm` (idle first when already in HighLevel, then HighLevel and the `visual_servo` profile) and `ekf_hold` (`reset_ekf`), then `reset_filters`, `button_in_view` and `check_reach`. After a walk the body EKF's yaw drifts up to 0.35 deg/s and drags the target in `foot_world`, so each stance resets it before the checks. `button_in_view` seeds the tracker, checks the box and the 3D pose, and picks the arm: the left arm only when the button is more than `press_arm_min_y` to the robot's left in `pelvis`. `check_reach` asks ik_server whether that arm reaches the press goal plus the offset in force, without moving the arm. A failed look calls `manipulation_recovery` (`stance_check`). After 4 failed rounds the slot releases the arms and says `press_ask`.
3. **Presses** (`press_and_recover`), up to 4. Each runs `set_press_offset`, `reset_filters`, `press_button` and `press_judge`. Nothing between the check and the first press walks, changes the mode or resets the EKF, so the press uses the `foot_world` the check used.
4. **Before each press after a failed one** `recover_once` calls `manipulation_recovery` (`diagnose`) on that failure, unless an executor rule answers first (below), so a slot makes at most 3 diagnoses and none after its fourth failed press. A decision that goes on presses again (a `reposition` first walks and looks, up to 3 times); `ask_help`, `abort`, a call without a decision, or a `reposition` whose looks all fail stop the presses (`recovery_stop`).

Pressing needs the bridge: every press is judged by `press_judge`, so with no agent, or with the bridge unreachable, each press counts as failed and the slot ends asking a person.

## The bridge link

- **Executor parameters**: `agent_backend` (`push`, or `none`: agent skills then fail), `agent_push_host`, `agent_push_port` (8771). The launch sets the host and port from the site's `agent_bridge` (`orchestrator/config/sites/<site>.yaml`); with no host set, agent skills fail. Running the bridge: `mission_control/README.md`.
- **One TCP link** (`PushLink`, `orchestrator/orchestrator/agent_push.py`): JSON with the images after it, framed by `agent_spec.HEADER`. Both ends ping every `PING_S` (5 s); a link silent for `DEAD_S` (20 s) is opened again, and the open call is sent again on it. The executor logs each connect and drop (`agent_link` in the trace, and its log, where the robot scripts read it).
- **Unreachable bridge**: with no word from it, the robot says "I lost the connection to my helper. Waiting for it to come back." after `UNREACHABLE_SAY_S` (15 s) and the call fails after `UNREACHABLE_FAIL_S` (60 s). Later calls fail at once until the bridge is heard from again, so the tree is not held a minute at each agent leaf.
- **Protocol**: each call carries `agent_spec.PROTOCOL`; a bridge with another value fails the call with both numbers.
- **The model**: `--model` (default `claude-opus-5-5`) at `--effort` (default `low`), through the Claude Agent SDK, with no tools, no setting sources, and a JSON schema of one answer (`agent_spec.schema`). One warm model process waits per kind of call. The bridge's `MODEL_TIMEOUT` is 120 s per answer.
- **Refusals**: a decision the executor refuses goes back to the model with the reason ("Decide again, same schema"). The third refused decision fails the call (`MAX_REFUSED` in `agent_push.py`; the bridge's `MAX_RETRIES` 2).
- **Record**: each call goes out on `/orchestrator/agent/request` (opened, then closed), each reply on `/orchestrator/agent/reply`, and the bridge's `started` (model, effort, prompt, system prompt, its sha256, the sha256 of `agent_prompts.py`), each decision with its usage, `failed`, and a cancel on `/orchestrator/agent/event`. The run recorder writes each call to the run's `agent/<call id>/` (`docs/recorder.md`).

## What the call carries

`Agent.start` gathers the evidence once per call; `open_request` (`agent_push.py`) builds the call.

| Field | What it holds |
|---|---|
| `id`, `state`, `protocol`, `skill`, `phase`, `opened`, `task` | The call; `task` is the same call as text (`_task` in `agent.py`), for the record |
| `button`, `question`, `base_goal` | The leaf's `target_description`, `question` (`operator_confirm`) and `base_goal` (the panel place, `map`) |
| `decisions`, `limits` | `PHASE_DECISIONS[phase]` and `agent_spec.limits()` |
| `failure` | `context.last_failure`: the last failed action call or check, uncut, with every feedback sample (`MAX_FEEDBACK` in `ros_skill_runner.py`). A `not_pressed` is recorded as a failed check whose `checked_call` is the judged press |
| `history` | The tree's last `MAX_HISTORY` (50) records, oldest first, without feedback: finished calls, every `check_reach` answer (`record: true`), and every agent decision with `phase`, `offset`, `goal`, `base_goal`, `left_arm`, `rule`, `rule_name` |
| `offset_in_force` | The offset the last press used (from its capture), else the last `set_press_offset` since the last press judged pressed, else 0 |
| `press` | `judge`, and `diagnose` after a judged miss (`press_evidence` in `ros_skill_runner.py`): `left_arm`, `goal` (press goal, `foot_world`), `press_offset_m`, `capture` (`pregrasp_t`, `deepest_t`, `release_t`, `max_depth_m`, `lateral_at_max_m`, `residual_m`, frame times, boxes), `frames` (name to saved path) |
| `stance` | `diagnose` and `stance_check`: `stance.evidence` (`stance.py`), or `{error}` when a transform was missing |
| images | The press frames named in `press`, and for `manipulation_recovery` the latest camera frame, saved as `call_<id>_camera.jpg` in `capture_dir` |

`stance` holds `stance_from_base_goal` (`pelvis` against the base goal: `forward_m`, `left_m`, `yaw_rad`); `target_in_pelvis` (the last `current_goal` of the failed or judged press, none in `stance_check`); `left_arm`; `reference` (the latest press of this button judged pressed with this arm whose target lies within `REFERENCE_DISTANCE_M` of this target in `map`, or of the base goal before any press); `match` (the stance that puts the target where the reference had it in `pelvis`); `other_arm` (the same for the other arm, with `target_offset_m`); `to_base_goal`; `reach` (the last `check_reach` since the last walk, with `target_moved_since_check_m`); `reach_move` (the reposition that cancels the horizontal IK miss plus `REACH_MARGIN_M`, with `fixable`, `within_limits`, `step_pelvis_m`); and a `note`.

What the model sees is less than the call carries:

- **Judge** (`judge_prompt`): the button, the panel type (from the button's name), the arm, which frames are attached and which are missing, `max_depth_m` and `lateral_at_max_m`; then the images in the order `press`, `press_crop`, `release`, `release_crop`, `baseline`, `baseline_crop`, each after its label. Not the failure, the history or `residual_m`.
- **Recovery** (`recovery_prompt`): the failure message (and the judged press's, so a judge's reason reaches the diagnosis), the box numbers of a failed `button_in_view`, the arm and the offset in force, the press capture and the suggested offset (offset in force minus residual), the stance, the target in `pelvis`, the button in the base-goal frame, the reference and the detection check (`SUSPECT` or consistent), the candidate stances (`Match`, `Nominal`, and in `stance_check` `Reference stance` and `Back-off stance`, then `Base goal`) with their distance and whether they are inside the limits, `Other arm`, the reach check and its reach move, and the last 12 history records without `QUIET_SKILLS`. Only the camera image is attached; the press frames are not. The feedback samples are not shown.

## press_judge

`press_button` can report `Controller reached target` with the fingertip far short (67 mm once), so every press is judged from its frames.

**The frames** (`orchestrator/orchestrator/press_capture.py`, configured by `interface.capture` in `press_button.yaml`). The phase tracker samples TF `right_ee` or `left_ee` in the goal's frame against the feedback's `current_goal`:

- `baseline`: the first frame once the end effector reaches the pre-grasp, `approach_offset_m` (0.10 m) before the offset goal.
- `press`: the first frame at least `press_delay_s` (5 ms) after the deepest point along the button normal. The camera runs at 6 fps, so it can come up to about 0.17 s late.
- `release`: the first frame at least `release_delay_s` (0.5 s) after the retreat began (depth `retreat_drop_m` below its deepest, or the action ended).
- `<name>_crop`: each frame cropped around track_anything's box for that frame, grown `crop_scale` (2.5) times; a frame without its own box uses the baseline's, with neither there is no crop.

The phase tracker counts only samples within `lateral_tolerance_m` of the button axis until the pre-grasp, because counting the arm's swing up from rest once reported 19 to 23 cm sideways misses after good presses. Before the call the executor waits up to 8 s for the frames to be chosen (`press_evidence`), then saves them as `press_<time>_<name>.jpg` in `capture_dir` (default `~/.ros/orchestrator_captures`); the recorder moves them into the run. Frame times are the camera's stamps and pose times the NUC's, so the Orin's and the NUC's clocks must agree.

**The rules** (`JUDGE_SYSTEM`): rule 1, the requested button's own light decides on both panels (lit in `press` or `release`: `pressed`; dark in both: `not_pressed`); rule 2, a light already on in `baseline` is `pressed`; rule 3, only the requested button's own light counts (not the card reader, the floor display or other buttons); rule 4, no frame shows that light: `not_pressed`. The reason names the light per frame, where the fingertip sits, and the rule number. Floor buttons light a blue ring at once, still visible in the release frame; the call button lights when the call registers, and the release frame often shows it when the hand covers it in the press frame.

`decide()` checks only that the decision is allowed and has a reason; no executor rule answers a judge call.

**The verdict.** `pressed`: the slot ends, the arms are released, and the executor appends a press reference (button, arm, robot pose, target in `map` and `pelvis`) to `~/orch_state/press_references.jsonl` on the NUC (the old `~/.ros/orchestrator_press_references.jsonl` is read while the new file does not exist). Later `diagnose` and `stance_check` calls read it as `reference`, `match` and `other_arm`. `not_pressed`, or a judge call that fails without a decision: the press counts as failed and `recover_once` runs. A wrong `not_pressed` costs a press; a wrong `pressed` leaves the button unpressed and the task waiting.

## manipulation_recovery

### Executor rules: decided without the model

Answers go in the history with `rule: true`, `rule_name`, and a reason starting `rule:`; they do not count toward `max_rounds`. In `diagnose` the executor tries `rule_offset`, then `rule_stance`; in `stance_check`, `rule_stance`, then `rule_reach`. Their thresholds are constants at the top of `agent.py`.

| Rule | Phase | When | Decision |
|---|---|---|---|
| `rule_offset` | `diagnose` | The first diagnosis since the last press judged pressed, after a `not_pressed` whose press missed by `RULE_MISS_M` (5 to 50 mm) in the button plane, or hit it in the plane but stopped more than `RULE_SHORT_M` (3 mm) short | `offset_retry`: offset in force minus `residual_m`, plus `RULE_DEPTH_MARGIN_M` (5 mm) deeper when short; not taken when an axis would pass `MAX_PRESS_OFFSET_M` |
| `rule_stance` | both | The robot stands more than `STANCE_TOLERANCE_M` or `STANCE_TOLERANCE_RAD` off its target stance (the last reposition since the last press judged pressed, else the base goal; the base goal again after a `continue` that came after that reposition, as the tree walks there); at most `STANCE_RULE_MAX` (2) times since then | `reposition` to that stance |
| `rule_reach` | `stance_check` | `check_reach` failed and its `reach_move` is `fixable` and `within_limits`; at most `REACH_RULE_MAX` (2) times since the last press judged pressed | `reposition` to `reach_move` |

The aim error repeats per panel and arm, so a miss is corrected by the offset in force minus the residual; floor buttons also lose a few mm to the waist giving on contact, hence deeper when short. `reach_move` is not fixable when the horizontal miss is at most `REACH_TOLERANCE_M` (an orientation miss) or the vertical miss passes `REACH_MAX_Z_M` (`stance.py`).

### AgentLeaf gates: failed before any call

`AgentLeaf._start_call` fails the call at once when no agent is configured; when an earlier decision of the same skill for the same `base_goal` was `ask_help`; or, in `diagnose`, when `max_rounds` (contract input, default 5) diagnoses not decided by a rule are in the history (across both panels, within its 50 records).

### decide() refusals

`AgentSession.decide` (`agent.py`) refuses, with a reason the model sees:

- a decision not in `PHASE_DECISIONS[phase]`, or an empty reason;
- `reposition` without a base goal, or without `dx` and `dy` (`dyaw` defaults to 0);
- `reposition` in `diagnose` after a passed `check_reach` from this stance when the press target moved under `STANCE_CHANGED_M` in `pelvis` since the check (`stance_unchanged`);
- `reposition` past `REPOSITION_LIMITS_M` from the base goal (forward, back, side);
- `switch_arm` without `use_left_arm`;
- `offset_retry` without all of `ox`, `oy`, `oz`, or with an axis past `MAX_PRESS_OFFSET_M`.

### What the tree does with each decision

`decision_outputs` (`agent.py`) gives the leaf `decision`, `reason`, `goal` (the base goal, moved for a reposition), `approach`, `walk_in`, `left_arm` and `offset_x/y/z`.

| Decision | Phase | The tree |
|---|---|---|
| `continue` | `stance_check` | Runs `look_here` from where the robot stands (the EKF reset only if this stance has none); presses only if both checks pass, else the round counts as failed. `goal` is the base goal again, so the next round walks there, and `rule_stance` takes the base goal as the target stance |
| `reposition` | `stance_check` | Ends the round; the next round walks to the new goal and looks again |
| `reposition` | `diagnose` | Releases the arms and runs up to 3 looks at the new goal, then presses again. When all 3 fail the robot says "I lost the button after moving. Stopping the task." and the slot fails without asking for help |
| `offset_retry` | `diagnose` | Presses again with the new offset |
| `retry` | `diagnose` | Presses again with the same offset and arm |
| `switch_arm` | `diagnose` | Presses again with the other arm; a later look chooses the arm again |
| `ask_help` | `diagnose` | Stops pressing; releases the arms and says `press_ask`. Later `manipulation_recovery` calls at this panel fail at once |
| `abort` | both | Ends the whole task with the reason |

A call that fails without a decision (a gate, the bridge unreachable or failed, three refusals, the contract timeout) goes on like `ask_help` in `diagnose` and like a failed look in `stance_check`.

A reposition is the whole stance relative to the base goal, in its frame (x forward, y left), not a step from where the robot stands. When the new goal is under `WALK_MIN_M` from the robot, `walk_in` is `yes` and the look walks first to `approach`, `APPROACH_BACK_M` behind the goal, then onto it, because `navigate_to` ends a shorter walk without a step. Open-loop local moves on `/local_movement_cmd_vel` covered half to 2.4 times the command, so there is no local-move skill and a small correction walks out and back in.

### How the model is instructed

The system prompt is `recovery_system(limits)`, built with the limits of the call, so a limit changed on the NUC reaches the model. Map a decision's reason to these ids:

| Ids | Covers | The executor checks it |
|---|---|---|
| Hard rule 1 | Reposition limits | Yes, `REPOSITION_LIMITS_M` |
| Hard rule 2 | Give the stance, never the walk-out | It plans the walk-out (`WALK_MIN_M`, `APPROACH_BACK_M`) |
| Hard rule 3 | No reposition after a passed `check_reach` and an unchanged target | Yes, `STANCE_CHANGED_M` |
| Hard rule 4 | Offset per axis | Yes, `MAX_PRESS_OFFSET_M` |
| Hard rule 5 | `switch_arm` only when `Other arm` is within 0.05 m of this target | No |
| Hard rule 6 | No stance from another button or panel | No |
| Hard rule 7 | Reach ahead of the pelvis in presses that worked; nothing past forward limit plus `REACH_AHEAD_MAX_M` | No |
| D1 | Detection suspect: `retry` once since the last walk, else `ask_help`; never reposition toward it | No |
| D2 | Aim miss after a reached goal: `offset_retry` with the suggested offset | Offset limit only |
| D3a | `target out of reach` after a passed check and an unchanged target: `retry` once, then `ask_help` | Via hard rule 3 |
| D3b | Otherwise `reposition` to `Match`, else `Nominal`, inside the limits, else `ask_help` | Limits only |
| D4 | Two failed repositions, or a repeat within 0.05 m: `ask_help` | No |
| D5 | No or lost track with the button in frame: `retry`, at most twice per task | No |
| D6 | Nothing fits: `ask_help` | No |
| S1 | Reach check failed: `reposition` to `Reach move` when fixable and within limits, else `continue` | `rule_reach` usually answers first |
| S2 | Too close (box at the bottom edge, or no 3D pose): `reposition` to `Back-off stance` | Limits only |
| S3 | Box at a side edge: `Reference stance` (`Match` needs a press target, which a `stance_check` call never has), else shift `dy` by `SIDE_SHIFT_M` | Limits only |
| S4 | No track or box but the button in the camera image: `continue` | No |
| S5 | Button not in the camera image: `abort` | No |

D3a's reason is the press start: the check plans from the arm at rest, the press starts wherever the arm is (after an earlier press it may still be raised). The reason must quote the numbers used and name the deciding step. The numbers the executor does not check are constants at the top of `agent_prompts.py`: `DETECTION_JUMP_M`, `BACK_OFF_M`, `SIDE_SHIFT_M`, `NOMINAL_TARGET_M`, `REACH_AHEAD_MAX_M`.

## operator_confirm

The bridge never asks the model. It rings the bell and prints the `question` input word for word; the operator types `y` or `n` (or `yes`, `no`, any case), an optional reason, and Enter. Other lines ask again; lines typed before the question do not count. The reason sent is `Operator: <their words>`, or the word itself. `confirmed` succeeds, `not_confirmed` fails. With no reply the bridge drops the question after `CONFIRM_S` (120 s, the contract's `timeout_s`) and the leaf fails. The bridge must run in a terminal the operator can type in; with stdin closed it cannot answer (`mission_control/robot/README.md`).

## Speech

The executor publishes one short sentence at a time on `/speak` (`std_msgs/String`; unitree_interface's TTS, `~/tts` remapped), and traces each as `agent_say`:

| When | Source |
|---|---|
| A model call opens | `PHASE_SPEECH` in `agent.py` |
| A decision comes | `DECISION_SPEECH` in `agent.py` (`ask_help` has none; the tree says `press_ask`) |
| A rule answers | "The press just missed the button. Correcting my aim and pressing again." (`rule_offset`), "I am not standing where I should. Walking to my spot again." (`rule_stance`), "The button is just out of my reach. Stepping closer to it." (`rule_reach`) |
| A call fails without a decision | `AgentLeaf._say_failure`: "I have no helper connected.", "I have used up my recovery attempts for this task.", "My helper is still not connected. Going on without it.", "My helper did not answer in time." (contract timeout), "My helper could not decide." (anything else); silent after an `ask_help` at this panel |
| The bridge goes quiet | `UNREACHABLE_SPEECH` in `agent_push.py`, after 15 s |
| The tree | `ekf_hold`: "Holding still to reset my balance estimate."; `press_ask`; the lost-stance sentence above |

## Limits

`agent_spec.py` holds the limits the executor enforces; every call carries them in `limits`, and the prompt states them.

| Constant | Value | Used by |
|---|---|---|
| `REPOSITION_LIMITS_M` | 0.15 m forward, 0.60 m back, 0.25 m side, from the base goal | `decide`, `reach_move.within_limits`, hard rule 1 |
| `MAX_PRESS_OFFSET_M` | 0.05 m per axis | `decide`, `rule_offset`, hard rule 4 |
| `STANCE_CHANGED_M` | 0.03 m of target movement in `pelvis` | `decide` (`stance_unchanged`), hard rule 3 |
| `STANCE_TOLERANCE_M`, `STANCE_TOLERANCE_RAD` | 0.06 m, 0.15 rad | `rule_stance` |
| `WALK_MIN_M`, `APPROACH_BACK_M` | 0.10 m, 0.15 m | `decision_outputs` (`walk_in`, `approach`), hard rule 2 |
| `max_rounds` | 5 (contract input) | `AgentLeaf` gate |

Elsewhere: the rule thresholds in `agent.py`, the reach-move and reference constants in `stance.py`, the prompt-only numbers in `agent_prompts.py`, the capture settings in `press_button.yaml` and `CaptureConfig` (`press_capture.py`), the link timings in `agent_push.py`, `MODEL_TIMEOUT` and `CONFIRM_S` in `agent_bridge.py`, and the round counts (4 looks, 4 presses, 3 looks after a reposition) in `press.xml`.

## Failure messages from this repository

The arm planner's own messages (`Planning failed: ...` and the like) come from manipulation_stack and are not listed here.

| Message | Where | Meaning |
|---|---|---|
| `press_button: no set_press_offset since the executor started; run it first (x=0 y=0 z=0 for no offset)` | `ros_skill_runner.py` | The executor refused the press; move_to_pose may hold an offset from before |
| `goal rejected` | `ros_skill_runner.py` | The action server refused the goal |
| `timeout after 60 s` | `skill_leaf.py` | `press_button`'s contract timeout ended it; read the feedback in `failure` |
| `<arm> arm, target out of reach: the arm ends X mm and Y deg from it; miss in the pelvis frame ...` | `check_reach` | IK from the arm at rest, on the press goal plus the offset in force; a pass reads `<arm> arm, in reach: ...` |
| `no target pose on /track_3d/selected_normal_pose_filtered/arm_upright within 2 s (...): look first` | `check_reach` | No fresh filtered goal, so no miss to walk by |
| `<arm> arm: <ik_server message>` | `check_reach` | No plan at all, so no miss to walk by |
| `10 s after seeding "<button>": no track, no 2D box, no 3D pose ...` | `button_in_view` | What was missing after the seed |
| `the target is at the edge of the image (...; needs 5%): ...` | `button_in_view` (`stance_checks.py`) | The box is within `margin_frac` of an edge; `outputs` has `box`, `image_size`, `centre_frac`, `edge_px` |

## Recoveries outside the agent

- Latched points in `/terrain_map` close ahead can block the walk into the cabin, so a failed walk in or out clears the terrain map and walks again, three times at most (`walk_in`, and `walk_out`'s walk on from the door, in `subtrees/walk.xml`).
- Tracking alone rejects the larger correction a ride leaves (0.5 m once), so `walk_out` stops about 1 m out of the door and relocalizes in place (`relocalize_here`) before walking on.

## Robot facts the evidence depends on

What the arm, the camera and the walk do underneath the skills: read it to interpret a call's failure, feedback and stance.

### The press pipeline

`press_button` calls the action `humanoid_arm_visual_servoing`:

1. **Seed.** Reset track_anything, then send the button text. The VLM gives a box; the tracker follows it. Each seed request waits 30 s, up to 2 requests.
2. **Target.** `arm_pose_filter` moves the pose into `foot_world`, filters it and publishes the upright goal.
3. **Plan.** move_to_pose plans in `pelvis`: IK for the pre-grasp, a joint-space path to it, a straight final leg to the press pose, then a retreat.
4. **Replan.** It replans when the target moves 0.01 m or 0.05 rad. The final leg freezes the goal in `left_ankle_roll_link` and runs in 4 steps, each re-mapped to the leaning pelvis.
5. **Done.** `Target reached`, then `Controller reached target`. This does not prove the button was pressed: `press_judge` decides that.

### Pose conventions

- **Normal pose** (track_anything): plane centroid, +X out of the panel toward the camera.
- **Upright goal**: same position, yaw-only, +X into the panel. This is `current_goal` and the press pose.
- **Pre-grasp**: the upright goal moved 0.10 m along its own −X. Both poses must be reachable; the pre-grasp can hit the torso when the panel is near.
- **Press offset**: `arm_safety_distance_{x,y,z}` on move_to_pose shifts every target in its own frame (x deeper, y left, z up). The press slot sets the panel's base offset (0 at the call panel, +3 mm on each axis inside) before the looks, and `set_press_offset` sets it again before every press: the base offset, then the last `offset_retry`. It resets to 0 on a relaunch. `press_button` refuses to run until a `set_press_offset` has run since the executor started. `check_reach` adds the offset of the last `set_press_offset` to its target itself, since ik_server keeps its launch offset (0).
- **Residual** (`residual_m`): fingertip minus goal at the deepest point, in the goal's frame made level: x along the goal's +X flattened to the horizontal (into the panel), z up, y = z cross x (left). Minus the residual is the correction.

### Topics and TF

| Name | Holds |
|---|---|
| `/track_3d/selected_normal_pose` | Raw target pose, camera frame, +X out of the panel |
| `/track_3d/selected_normal_pose_filtered/arm_upright` | Filtered upright goal the arm is sent, `foot_world` |
| `/track_3d/is_tracking` | The 2D tracker holds a mask; a 3D pose may still be missing |
| `/track_3d/detections_2d` | The button's 2D box: `button_in_view` judges it, and the press capture crops around it |
| `/keyboard_input` | The button text that seeds track_anything (`button_in_view`; `press_button` seeds through the servo node) |
| `/track_3d/tracked_overlay` | Camera image with mask, box and tracker state |
| `/odom_base_link` | Robot pose in `map` |
| `/route_planner/status` | JSON: `state` (`routing`, `arrived`, `blocked`), `dist_to_goal` |
| `/camera/camera/color/camera_info` | Camera intrinsics |
| `/camera/camera/color/image_raw` | The color frames (RGB8, 1280x720 at 6 fps, raw only): the press frames and the camera image of a call |
| TF `pelvis`, `torso_link`, `foot_world` | Body frames; `body_ekf` publishes `foot_world` |
| TF `left_ee`, `right_ee` | End-effector points, 0.1847585 m along X of `*_wrist_yaw_link` |
| TF `camera_link` | Fixed on `torso_link`; the RealSense frames hang below it |
| TF `base_link` | Navigation base: `sensor` (the lidar on the torso) 0.35 m lower, with the lidar's yaw. `navigate_to` puts it on the goal; `stance` measures `pelvis`, 1 to 2 cm and about 2° plus the waist yaw away |
| Service `/check_reach` | ik_server: move_to_pose's IK verdict for a pose, without moving the arm; its miss in `pelvis` |

The live target is often gone by the time of a call. The servo node resets track_anything after a cancel, a timeout, a failed seed or a success. After `Planning failed` the tracker may still publish. So the executor takes the press target from the last `current_goal` in the failure record, not from the live topic.

### Arm facts

- Profile `arms_with_waist` locks the waist to ±0.0001 rad. The torso cannot lean.
- `g1.yaml` `joint_limit_overrides` tightens the shoulder limits. Use them over the URDF's.
- End effector: `offset: [0.1847585, 0, 0]` from `*_wrist_yaw_joint`. `collision_margin: -0.01`.
- The planner solves IK with CasADi in C++. `check_reach` asks the same IK (ik_server, from the live joint state), so its verdict matches the press planner's.
- Joints move at most 0.5 rad/s, 0.25 rad/s on the final leg.

### Perception facts

- `is_tracking` true means a 2D mask. The 3D pose can still be dropped (too few depth points, failed fit) with no message.
- A lost object shows `Lost (Ns)` in the overlay. After 20 s the tracker stops and does not re-query.
- The D435 gives no depth closer than about 0.28 m, so a button that close has no 3D pose.

### Navigation facts

- `navigate_to` succeeds when the local planner reports the robot within `goal_reached_threshold` 0.15 m and `goal_yaw_threshold` 0.15 rad. The `arrived` state on `/route_planner/status` comes earlier (within 2 m lookahead) and is not the arrival.
- A route `blocked` for 10 s fails with `state = blocked for 10 s`: the stance is off the route graph. Blocked from the first feedback with `dist_to_goal` inf points at the wrong storey.
- Goals carry the storey in z (about −0.46 m on the elevator floor). A reposition keeps the base goal's z.
- Walking needs HighLevel mode, `/stop` 0 and `/autonomy_mode` true. A look sets `/autonomy_mode` true for its walk and false after it; the mode is HighLevel from the stance's `prepare_arm`.
- Side and back steps are slower: 0.15 m/s.

### Where to check these facts on the NUC

Under `/home/vector/eigen_vector_ws`, for checking the facts below. Other workspaces are not in use.

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

track_anything runs on the Orin and its VLM on the Thor.

### Sources

The facts in this document were read from these versions. Update it when they change.

- manipulation_stack `pelvis-correction` @ `d70b9d1`; the NUC runs `test/pelvis-correction` @ `0554dd4`. Files: `humanoid_arm_skills/{humanoid_arm_visual_servoing,pose_filtering,utils}.py`, `g1_pilot/src/move_to_pose/{server,planning,control}.cpp`, `g1_pilot/src/core/arm_mp/{visual_servo_planner,joint_space_planner}.cpp`, `g1_pilot/config/g1.yaml`, `g1_description/assets/g1/*.urdf`
- track_anything `main` @ `f5a1f47`: `track_3d.py`, `track_3d_parameters.yaml`, `launch/track_3d.launch.py`
- navigation_stack `main` @ `6242b3e`: `route_planner_node.py`, `local_planner.cpp`, `{local_planner,route_planner}/config/unitree_g1.yaml`
- NUC: `src/bringup/launch/nuc.launch.py`
- orchestrator (`ltx-reorg`): `orchestrator/orchestrator/{agent,agent_spec,agent_push,stance,press_capture,stance_checks,ros_skill_runner,skill_leaf}.py`, `mission_control/{agent_bridge,agent_prompts}.py`, `skill_registry/contracts/{press_button,press_judge,check_reach,button_in_view,set_press_offset,manipulation_recovery}.yaml`, `skill_registry/trees/subtrees/{press,elevator}.xml`
