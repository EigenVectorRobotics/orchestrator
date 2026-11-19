# Agent skills

An agent skill hands one decision of a tree to a model or to the operator. There are three: `press_judge`, `manipulation_recovery` and `operator_confirm`. Each is a folder of `g1_skills/g1_skills/` whose `contract.yaml` has `transport: agent`, with `spec.py` (phases, decisions, decision fields, limits), `agent.py` (an `AgentSkill`: the evidence, the executor's rules and checks, the outputs) and, when the model answers it, `prompt.py`. How to make one is in `g1_skills/README.md`. The leaf is `AgentLeaf` (`orchestrator/orchestrator/tree/leaf.py`) and the runner is `Agent` (`orchestrator/orchestrator/agent/api.py`); neither knows a skill. The call's sequence is drawn in [Sequences](design/sequences.md#one-agent-call).

The agent bridge on the operator laptop (`mission_control/agent_bridge.py`) answers every call. For `press_judge` and `manipulation_recovery` it asks a Claude model that has no tools and reads no documents: it sees only the system prompt and the user message built in the skill's `prompt.py`. When this document and the prompt disagree, the prompt is what the model follows; when either disagrees with the code, the code is right. What the executor and the bridge share is written once: each skill's phases, decisions, decision fields and limits in its `spec.py`, and the link framing and protocol number in `orchestrator/orchestrator/agent/protocol.py`.

## The skills at a glance

| Skill | Phase | The tree calls it | Answered by | Decisions |
|---|---|---|---|---|
| `press_judge` | `judge` | Right after every `press_button` that succeeded | the model, `JUDGE_SYSTEM` | `pressed`, `not_pressed` |
| `manipulation_recovery` | `stance_check` | A look before the first press failed (`button_in_view`, `check_reach`, or any other step of the look) | the model, `recovery_system(limits)` | `continue`, `reposition`, `abort` |
| `manipulation_recovery` | `diagnose` | Before each press after a failed one: `press_button` failed, `press_judge` said `not_pressed`, or another step of the press failed | the model, `recovery_system(limits)` | `reposition`, `switch_arm`, `offset_retry`, `retry`, `ask_help` |
| `operator_confirm` | `confirm` | No tree on this branch calls it; the tests do (`g1_skills/test/test_agent_push.py`, `mission_control/test/test_agent_bridge.py`) | the operator, in the bridge's terminal | `confirmed`, `not_confirmed` |

`PHASES` in each skill's `spec.py` is the source for the decisions per phase, and each contract's `timeout_s` bounds a call. A decision in `GOING_ON` makes the leaf succeed; `not_pressed`, `not_confirmed`, `ask_help` and `abort` make it fail, and `abort` also fails the whole tree at once with the reason (`BTContext.abort`).

## The press slot

Each panel runs the press slot `press_panel_button` (`skill_registry/trees/subtrees/press.xml`; its states are drawn in [Trees](design/trees.md#the-press-slot)). The elevator subtrees `call_elevator` and `press_floor_button` (`skill_registry/trees/subtrees/elevator.xml`) set the panel first: `button`, `press_panel`, `press_left_arm`, `press_arm_min_y`, `press_base_offset_{x,y,z}` and `press_ask`, each with its reason in a comment there.

1. **Base offset.** `set_press_offset` with the panel's base offset, so `check_reach` checks the goal the first press aims at. An offset never carries over from one panel to the other.
2. **Stance check** (`stance_check` subtree), up to 4 rounds. Each round runs `look`: walk to `recovery_goal` (the panel place at first; first to `recovery_approach` when `recovery_walk_in` is `yes`), wait for the robot to settle, then `look_here`. `look_here` runs, once per stance, `prepare_arm` (Idle first when already in HighLevel, then HighLevel and the `visual_servo` profile) and `ekf_hold` (`reset_ekf`), then `reset_filters`, `button_in_view` and `check_reach`. After a walk the body EKF's yaw drifts and drags the target in `foot_world`, so each stance resets it before the checks. `button_in_view` seeds the tracker, checks the box and the 3D pose, and picks the arm: the left arm only when the button is more than `press_arm_min_y` to the robot's left in `pelvis`. `check_reach` asks ik_server whether that arm reaches the press goal plus the offset in force, without moving the arm. A failed look calls `manipulation_recovery` (`stance_check`). After 4 failed rounds the slot releases the arms and says `press_ask`.
3. **Presses** (`press_and_recover`), up to 4. Each runs `set_press_offset`, `reset_filters`, `press_button` and `press_judge`. Nothing between the check and the first press walks, changes the mode or resets the EKF, so the press uses the `foot_world` the check used.
4. **Before each press after a failed one** `recover_once` calls `manipulation_recovery` (`diagnose`) on that failure, so a slot makes at most 3 diagnoses and none after its fourth failed press. A decision that goes on presses again (a `reposition` first walks and looks, up to 3 times); `ask_help` or a call without a decision stops the presses (`recovery_stop`), and the slot asks a person. A `reposition` whose looks all fail ends the slot without asking (below).

The round and press counts are the `num_attempts` in `press.xml`. Pressing needs the bridge: every press is judged by `press_judge`, so with no agent, or with the bridge unreachable, each press counts as failed and the slot ends asking a person.

## The bridge link

The link's framing, pings, reconnects and the unreachable-bridge timing are described in the docstring of `orchestrator/orchestrator/agent/link.py`; running the bridge is in `mission_control/README.md`. What matters when you read a call:

- **Executor parameters**: `agent_backend` (`push`, or `none`: agent skills then fail), `agent_push_host`, `agent_push_port`. The launch sets the host and port from the site's `agent_bridge` (`orchestrator/config/sites/stanley.yaml`); with no host set, agent skills fail. The executor traces each connect and drop as `agent_link` and logs it, where the robot scripts read it.
- **Unreachable bridge**: with no word from it, the robot says `UNREACHABLE_SPEECH` after `UNREACHABLE_SAY_S` and the call fails after `UNREACHABLE_FAIL_S` (`link.py`). Later calls fail at once until the bridge is heard from again, so the tree is not held at each agent leaf.
- **Protocol**: each call carries `protocol.PROTOCOL`; a bridge with another value fails the call with both numbers.
- **The model**: the bridge's `--model` at `--effort`, through the Claude Agent SDK, with no tools, no setting sources, and a JSON schema of one answer (`protocol.schema`, from the skill's `FIELDS` and the phase's decisions). One warm model process waits per kind of call. `MODEL_TIMEOUT` (`agent_bridge.py`) bounds each answer.
- **Refusals**: a decision the executor refuses goes back to the model with the reason ("Decide again, same schema"). The `MAX_REFUSED`th refused decision fails the call (`link.py`; the bridge asks again at most `MAX_RETRIES` times).
- **Record**: each call goes out on `/orchestrator/agent/request` (opened, then closed), each reply on `/orchestrator/agent/reply`, and the bridge's `started` (model, effort, prompt, system prompt, its sha256, the sha256 of the skill's `prompt.py`), each decision with its usage, `failed`, and a cancel on `/orchestrator/agent/event`. The run recorder writes each call to the run's `agent/<call id>/` (`docs/recorder.md`).

## What the call carries

`Agent.start` gathers the evidence once per call: the failure and the history, then what the skill's `evidence()` adds. `open_request` (`link.py`) builds the call from the generic fields and the skill's `request_fields()`.

| Field | What it holds |
|---|---|
| `id`, `state`, `protocol`, `skill`, `phase`, `opened`, `task` | The call; `task` is the same call as text (`task_text`, with the skill's `task_lines()`), for the record |
| `button`, `base_goal`, `question` | The press skills: the leaf's `target_description` and, for `manipulation_recovery`, `base_goal` (the panel place, `map`); `operator_confirm`: `question` |
| `decisions`, `limits` | `PHASES[phase]` and `limits()` of the skill's `spec.py` |
| `failure` | `BTContext.last_failure`: the last failed action call or check, uncut, with its feedback samples (thinned past `MAX_FEEDBACK` in `orchestrator/orchestrator/skills/transports.py`). A `not_pressed` is recorded as a failed check whose `checked_call` is the judged press |
| `history` | The tree's last `MAX_HISTORY` records (`orchestrator/orchestrator/tree/engine.py`), oldest first, without feedback: finished action calls, every `check_reach` answer (`record: true`), and every agent decision with `phase`, `rule`, `rule_name` and, for `manipulation_recovery`, `offset`, `goal`, `base_goal`, `left_arm` |
| `offset_in_force` | The press skills: the offset the last press used (from its capture), else the last `set_press_offset` since the last press judged pressed, else 0 (`offset_in_force`, `g1_skills/g1_skills/lib/press.py`) |
| `press` | `judge`, and `diagnose` after a judged miss (`PressState.evidence`, `lib/press.py`): `left_arm`, `goal` (press goal, `foot_world`), `goal_axis`, `press_offset_m`, `capture` (`pregrasp_t`, `deepest_t`, `release_t`, `max_depth_m`, `lateral_at_max_m`, `residual_m`, `residual_from`, frame times, boxes), `frames` (name to saved path), `aim` (the aim crops, below; none without the intrinsics or a camera pose) |
| `stance` | `diagnose` and `stance_check`: `stance.evidence` (`g1_skills/g1_skills/manipulation_recovery/stance.py`), or `{error}` when a transform was missing |
| images | The press frames named in `press`; for `manipulation_recovery` also its aim crops and the latest camera frame, saved as `call_<id>_camera.jpg` in `capture_dir` |

`stance` holds `stance_from_base_goal` (`pelvis` against the base goal: `forward_m`, `left_m`, `yaw_rad`); `target_in_pelvis` (the last `current_goal` of the failed or judged press, none in `stance_check`); `left_arm`; `reference` (the latest press of this button judged pressed with this arm whose target lies within `REFERENCE_DISTANCE_M` of this target in `map`, or of the base goal before any press); `match` (the stance that puts the target where the reference had it in `pelvis`); `other_arm` (the same for the other arm, with `target_offset_m`); `to_base_goal`; `reach` (the last `check_reach` since the last walk, with `target_moved_since_check_m`); `reach_move` (the reposition that cancels the horizontal IK miss plus `REACH_MARGIN_M`, with `fixable`, `within_limits`, `step_pelvis_m`); and a `note`.

What the model sees is less than the call carries:

- **Judge** (`judge_prompt`): the button, the panel type (from the button's name), the arm, which frames are attached and which are missing, `max_depth_m` and `lateral_at_max_m`; then the images in the order `press`, `press_crop`, `release`, `release_crop`, `baseline`, `baseline_crop`, each after its label. Not the failure, the history or `residual_m`.
- **Recovery** (`recovery_prompt`): the phase, the button and its panel type, the allowed decisions and the base goal; the failure message (and the judged press's, so a judge's reason reaches the diagnosis), the box numbers of a failed `button_in_view`, the arm and the offset in force, the press capture and the residual-based offset (offset in force minus residual, from the gripper estimate or the encoders); the aim crops with each one's mm per pixel and image right and down in the press frame; the stance, the target in `pelvis`, the button in the base-goal frame, the reference and the detection check (`SUSPECT` or consistent); the candidate stances (`Match`, `Nominal`, and in `stance_check` `Reference stance` and `Back-off stance`, then `Base goal`) with their distance, how the executor walks there and whether they are inside the limits; `Other arm`; the reach check and its reach move; and the last 12 history records without `QUIET_SKILLS`, with the totals of repositions and retries. The aim crops (`contact_aim`, then `before_aim`) and the camera image are attached; the press frames are not. The feedback samples are not shown.

## press_judge

`press_button` can report `Controller reached target` with the fingertip far short (67 mm once), so every press is judged from its frames.

**The frames** (`g1_skills/g1_skills/press_button/capture.py` and `skill.py`, configured by `config.capture` in `press_button`'s contract and the defaults of `CaptureConfig`). The phase tracker samples TF `right_ee` or `left_ee` in the goal's frame against the feedback's `current_goal`:

- `baseline`: the first frame once the end effector reaches the pre-grasp, `approach_offset_m` before the offset goal.
- `press`: the first frame at least `press_delay_s` after the deepest point along the button normal. The camera's frame rate bounds how late it can come.
- `release`: the first frame at least `release_delay_s` after the retreat began (depth `retreat_drop_m` below its deepest, or the action ended).
- `<name>_crop`: each frame cropped around track_anything's box for that frame, grown `crop_scale` times; a frame without its own box uses the baseline's, with neither there is no crop.
- `before_aim`, `contact_aim` (the aim crops, `aim_view`): the baseline and press frames cut around the press goal projected into the image (`AIM_HALF_WIDTH_PX`, `AIM_HALF_HEIGHT_PX`, upscaled `AIM_SCALE` times), each with its mm per pixel at the button and the image's right and down in the press frame. They need the camera intrinsics (`camera_info_topic`) and the camera's pose in the goal's frame near each frame's time (TF from the image's optical frame), sampled with the end effector.

**The residual** (`residual_m`): fingertip minus goal at the deepest point, in the press frame: the goal's frame made level (`press_axes`), x along the goal's +X flattened to the horizontal (into the panel), z up, y = z cross x (left). Minus the residual is the correction. The skill samples the gripper's depth fit (TF `<gripper>_estimate`, `right_gripper_frame`, `left_gripper_frame`) against the encoder gripper at the fit's stamp; a fit near the deepest point moves the residual to the real fingertip (`residual_from` `gripper estimate`; else `encoders`). Not yet tested on the robot.

The phase tracker counts only samples within `lateral_tolerance_m` of the button axis until the pre-grasp, because counting the arm's swing up from rest once reported 19 to 23 cm sideways misses after good presses. Before the call the executor waits for the frames to be chosen (`PressState.evidence`, its `wait_s`), then saves them as `press_<time>_<name>.jpg` in `capture_dir`; the recorder moves them into the run. Frame times are the camera's stamps and pose times the NUC's, so the Orin's and the NUC's clocks must agree.

**The rules** (`JUDGE_SYSTEM`): rule 1, the requested button's own light decides on both panels (lit in `press` or `release`: `pressed`; dark in both: `not_pressed`); rule 2, a light already on in `baseline` is `pressed`; rule 3, only the requested button's own light counts (not the card reader, the floor display or other buttons); rule 4, no frame shows that light: `not_pressed`. The reason names the light per frame, where the fingertip sits, and the rule number. Floor buttons light a blue ring at once, still visible in the release frame; the call button lights when the call registers, and the release frame often shows it when the hand covers it in the press frame.

`AgentSession.decide` checks only that the decision is allowed and has a reason; no executor rule answers a judge call.

**The verdict.** `pressed`: the slot ends, the arms are released, and `press_judge`'s `after_decision` appends a press reference (button, arm, robot pose, target in `map` and `pelvis`) to `DEFAULT_REFERENCES` (`stance.py`, under `STATE_DIR` on the NUC; the old `OLD_REFERENCES` file is read while the new one does not exist). Later `diagnose` and `stance_check` calls read it as `reference`, `match` and `other_arm`. `not_pressed`, or a judge call that fails without a decision: the press counts as failed and `recover_once` runs. A wrong `not_pressed` costs a press; a wrong `pressed` leaves the button unpressed and the task waiting.

## manipulation_recovery

### Executor rules: decided without the model

Answers go in the history with `rule: true`, `rule_name`, and a reason starting `rule:`; they do not count toward `max_rounds`. In `diagnose` the executor tries `rule_stance`; in `stance_check`, `rule_stance`, then `rule_reach`. A missed press always goes to the model, with its aim crops. The rules and their thresholds are in `g1_skills/g1_skills/manipulation_recovery/rules.py`.

| Rule | Phase | When | Decision |
|---|---|---|---|
| `rule_stance` | both | The robot stands more than `STANCE_TOLERANCE_M` or `STANCE_TOLERANCE_RAD` off its target stance (the last reposition since the last press judged pressed, else the base goal; the base goal again after a `continue` that came after that reposition, as the tree walks there); at most `STANCE_RULE_MAX` times since the last press judged pressed | `reposition` to that stance |
| `rule_reach` | `stance_check` | `check_reach` failed and its `reach_move` is `fixable` and `within_limits`; at most `REACH_RULE_MAX` times since the last press judged pressed | `reposition` to `reach_move` |

The aim error repeats per panel and arm, so the model corrects a miss with the offset in force minus the miss it reads off `contact_aim` (O1 to O4 below); floor buttons also lose a few mm to the waist giving on contact, hence deeper when short. `reach_move` is not fixable when the horizontal miss is at most `REACH_TOLERANCE_M` (an orientation miss) or the vertical miss passes `REACH_MAX_Z_M` (`stance.py`).

### AgentLeaf gates: failed before any call

`AgentLeaf._start_call` fails the call at once when no agent is configured. It then asks the skill's `refuse()`: `manipulation_recovery` refuses when an earlier decision of the same skill for the same `base_goal` was `ask_help`, or, in `diagnose`, when `max_rounds` (contract input) diagnoses not decided by a rule are in the history (across both panels, within its `MAX_HISTORY` records).

### Refused decisions

`AgentSession.decide` (`orchestrator/orchestrator/agent/api.py`) and the skill's `check()` (`manipulation_recovery/agent.py`) refuse, with a reason the model sees:

- a field the skill's `FIELDS` does not have, a decision not in `PHASES[phase]`, or an empty reason;
- `reposition` without a base goal, or without `dx` and `dy` (`dyaw` defaults to 0);
- `reposition` in `diagnose` after a passed `check_reach` from this stance when the press target moved under `STANCE_CHANGED_M` in `pelvis` since the check (`stance_unchanged`);
- `reposition` past `REPOSITION_LIMITS_M` from the base goal (forward, back, side);
- `switch_arm` without `use_left_arm`;
- `offset_retry` without all of `ox`, `oy`, `oz`, or with an axis past `MAX_PRESS_OFFSET_M`.

### What the tree does with each decision

`decision_outputs` (`manipulation_recovery/rules.py`) gives the leaf `decision`, `reason`, `goal` (the base goal, moved for a reposition), `approach`, `walk_in`, `left_arm` and `offset_x/y/z`.

| Decision | Phase | The tree |
|---|---|---|
| `continue` | `stance_check` | Runs `look_here` from where the robot stands (the EKF reset only if this stance has none); presses only if both checks pass, else the round counts as failed. `goal` is the base goal again, so the next round walks there, and `rule_stance` takes the base goal as the target stance |
| `reposition` | `stance_check` | Ends the round; the next round walks to the new goal and looks again |
| `reposition` | `diagnose` | Releases the arms and runs up to 3 looks at the new goal, then presses again. When all 3 fail the robot says it lost the button and is stopping, and the slot fails without asking for help |
| `offset_retry` | `diagnose` | Presses again with the new offset |
| `retry` | `diagnose` | Presses again with the same offset and arm |
| `switch_arm` | `diagnose` | Presses again with the other arm; a later look chooses the arm again |
| `ask_help` | `diagnose` | Stops pressing; releases the arms and says `press_ask`. Later `manipulation_recovery` calls at this panel fail at once |
| `abort` | `stance_check` | Ends the whole task with the reason. Not in `diagnose`: no step of D1 to D6 ends the task, and `ask_help` there stops the presses and asks a person |

A call that fails without a decision (a gate, the bridge unreachable or failed, too many refusals, the contract timeout) goes on like `ask_help` in `diagnose` and like a failed look in `stance_check`.

A reposition is the whole stance relative to the base goal, in its frame (x forward, y left), not a step from where the robot stands; it keeps the base goal's z (`offset_goal`). When the new goal is under `WALK_MIN_M` from the robot, `walk_in` is `yes` and the look walks first to `approach`, `APPROACH_BACK_M` behind the goal, then onto it, because `navigate_to` ends a shorter walk without a step. Open-loop local moves on `/local_movement_cmd_vel` covered half to 2.4 times the command, so there is no local-move skill and a small correction walks out and back in.

### How the model is instructed

The system prompt is `recovery_system(limits)`, built with the limits of the call, so a limit changed on the NUC reaches the model. Map a decision's reason to these ids:

| Ids | Covers | The executor checks it |
|---|---|---|
| Hard rule 1 | Reposition limits | Yes, `REPOSITION_LIMITS_M` |
| Hard rule 2 | Give the stance, never the walk-out | It plans the walk-out (`WALK_MIN_M`, `APPROACH_BACK_M`) |
| Hard rule 3 | No reposition after a passed `check_reach` and an unchanged target | Yes, `STANCE_CHANGED_M` |
| Hard rule 4 | Offset per axis | Yes, `MAX_PRESS_OFFSET_M` |
| Hard rule 5 | `switch_arm` only when `Other arm` is near this target | No |
| Hard rule 6 | No stance from another button or panel | No |
| Hard rule 7 | Reach ahead of the pelvis in presses that worked; nothing past the forward limit plus `REACH_AHEAD_MAX_M` | No |
| D1 | Detection suspect: `retry` once since the last walk, else `ask_help`; never reposition toward it | No |
| D2 | Aim miss after `Controller reached target`, the fingertip a few mm off or short of the face in the aim crops (or, without them, the residual): `offset_retry` | Offset limit only |
| O1 to O4 | Setting the offset: the miss of the fingertip from the face centre in `contact_aim`, in the press frame, moves `oy` and `oz`; `ox` goes deeper by the shortfall plus a margin when the residual says the press stopped short on the face; without crops, the residual-based offset | Offset limit only |
| D3a | `target out of reach` (or an aim miss too large for an offset) after a passed check and an unchanged target: `retry` once, then `ask_help` | Via hard rule 3 |
| D3b | Otherwise `reposition` to `Match`, else `Nominal`, inside the limits, else `ask_help` | Limits only |
| D4 | Two failed repositions, or a repeat of a failed one: `ask_help` | No |
| D5 | No or lost track with the button in frame: `retry`, at most twice per task | No |
| D6 | Nothing fits: `ask_help` | No |
| S1 | Reach check failed: `reposition` to `Reach move` when fixable and within limits, else `continue` | `rule_reach` usually answers first |
| S2 | Too close (box at the bottom edge, or no 3D pose): `reposition` to `Back-off stance` | Limits only |
| S3 | Box at a side edge: `Reference stance` when listed and at least `WALK_MIN_M` from the robot (`Match` needs a press target, which a `stance_check` call never has), else shift `dy` by `SIDE_SHIFT_M` | Limits only |
| S4 | No track or box but the button in the camera image: `continue` | No |
| S5 | Button not in the camera image: `abort` | No |

D3a's reason is the press start: the check plans from the arm at rest, the press starts wherever the arm is (after an earlier press it may still be raised). The reason must quote the numbers used and name the deciding step. The numbers the executor does not check are constants at the top of `manipulation_recovery/prompt.py` (`DETECTION_JUMP_M`, `BACK_OFF_M`, `SIDE_SHIFT_M`, `NOMINAL_TARGET_M`, `REACH_AHEAD_MAX_M`) or written in `recovery_system` itself. D2 and O1 to O4 with the aim crops are not yet tested on the robot.

## operator_confirm

The bridge never asks the model. It rings the bell and prints the `question` input word for word; the operator types `y` or `n` (or `yes`, `no`, any case), an optional reason, and Enter. Other lines ask again; lines typed before the question do not count. The reason sent is `Operator: <their words>`, or the word itself. `confirmed` succeeds, `not_confirmed` fails. With no reply the bridge drops the question after `CONFIRM_S` (`agent_bridge.py`, kept equal to the contract's `timeout_s`) and the leaf fails. The bridge must run in a terminal the operator can type in; with stdin closed it cannot answer (`mission_control/robot/README.md`).

## Speech

The executor publishes one short sentence at a time on `/speak` (`std_msgs/String`; unitree_interface's TTS, `~/tts` remapped), and traces each as `agent_say`:

| When | Source |
|---|---|
| A model call opens | `phase_speech` of the skill's `agent.py` |
| A decision is accepted | `decision_speech` of the skill's `agent.py` (`ask_help` has none; the tree says `press_ask`) |
| A rule answers | `RULE_SPEECH` in `manipulation_recovery/agent.py` |
| A call fails without a decision | `AgentLeaf._say_failure` (no agent, the bridge unreachable, the contract timeout, anything else), or the second element of the skill's `refuse()` (`manipulation_recovery`: the spent budget; silent after an `ask_help` at this panel) |
| The bridge goes quiet | `UNREACHABLE_SPEECH` in `link.py` |
| The tree | `ekf_hold`, `press_ask` and the lost-stance sentence, in `press.xml` |

## Limits

`manipulation_recovery/spec.py` holds the limits the executor enforces; every call carries them in `limits`, and the prompt states them.

| Constant | What | Used by |
|---|---|---|
| `REPOSITION_LIMITS_M` | Forward, back and side, from the base goal | `check`, `reach_move.within_limits`, hard rule 1 |
| `MAX_PRESS_OFFSET_M` | Per axis | `check`, hard rule 4 |
| `STANCE_CHANGED_M` | Target movement in `pelvis` that counts as a new stance | `check` (`stance_unchanged`), hard rule 3 |
| `STANCE_TOLERANCE_M`, `STANCE_TOLERANCE_RAD` | Off the target stance | `rule_stance` |
| `WALK_MIN_M`, `APPROACH_BACK_M` | The shortest walk with a step; the walk-out behind a nearer goal | `decision_outputs` (`walk_in`, `approach`), hard rule 2, S3 |
| `max_rounds` | Contract input | `refuse` |

Elsewhere: the rule counts in `rules.py`, the reach-move and reference constants in `stance.py`, the prompt-only numbers in `prompt.py` (all in `manipulation_recovery/`), the capture settings in `press_button`'s `config.capture` and `CaptureConfig` (`press_button/capture.py`), the link timings in `link.py`, `MODEL_TIMEOUT` and `CONFIRM_S` in `agent_bridge.py`, and the round counts in `press.xml`.

## Failure messages from this repository

The arm planner's own messages (`Planning failed: ...` and the like) come from manipulation_stack and are not listed here. `<timeout_s>`, `<pose_topic>`, `<max_age_s>`, `<max_wait_s>` and `<margin_frac>` stand for those settings in the skill's contract.

| Message | Where | Meaning |
|---|---|---|
| `press_button: no set_press_offset since the executor started; run it first (x=0 y=0 z=0 for no offset)` | `press_button/skill.py` | The executor refused the press; move_to_pose may hold an offset from before |
| `goal rejected` | `orchestrator/orchestrator/skills/transports.py` | The action server refused the goal |
| `timeout after <timeout_s> s` | `orchestrator/orchestrator/tree/leaf.py` | The contract timeout ended the call; for `press_button` read the feedback in `failure` |
| `<arm> arm, target out of reach: the arm ends X mm and Y deg from it; miss in the pelvis frame ...` | `check_reach/skill.py` | IK from the arm at rest, on the press goal plus the offset in force; a pass reads `<arm> arm, in reach: ...` |
| `no target pose on <pose_topic> within <max_age_s> s (...): look first` | `check_reach/skill.py` | No fresh filtered goal, so no miss to walk by |
| `<arm> arm: <ik_server message>` | `check_reach/skill.py` | No plan at all, so no miss to walk by |
| `<max_wait_s> s after seeding "<button>": no track, no 2D box, no 3D pose ...` | `button_in_view/skill.py` | What was missing after the seed |
| `no fresh box of the target from track_anything (...)` | `button_in_view/skill.py` (`judge_view`) | Not tracking, or the button is out of the frame |
| `the target is at the edge of the image (...; needs <margin_frac>): ...` | `button_in_view/skill.py` (`judge_view`) | The box is within `margin_frac` of an edge; `outputs` has `box`, `image_size`, `centre_frac`, `edge_px` |

## Robot facts the evidence depends on

What the arm, the camera and the walk do underneath the skills: read it to interpret a call's failure, feedback and stance. These facts come from other repositories (versions at the end), not from this one. The topics each skill uses are in its contract's `interface` and `config`; the robot-side services the press needs are in `docs/machines.md`. Numbers are the code defaults; the launch files named below do not override them unless said.

### The press action

`press_button` calls the action `humanoid_arm_visual_servoing` (`humanoid_arm_visual_servoing.py`), which sends one goal to move_to_pose and lets it follow the target topic:

1. **Seed.** Reset track_anything, then send the button text. The VLM gives a box; the tracker follows it. A seed request waits `seed_timeout` (30 s), up to `seed_max_requests` (2). Once tracking, a filtered pose must come within `first_pose_timeout` (10 s).
2. **Target.** `arm_pose_filter` moves the pose into `foot_world`, filters it (Kalman by launch default) and publishes the upright goal.
3. **Plan.** move_to_pose solves in `pelvis`: a joint-space path to the pre-grasp, a 1 s pause (`PREGRASP_DELAY`), then a straight final leg to the press pose.
4. **Replan.** Until the final leg starts, it replans when the target moves `replan_position_delta` (0.01 m) or `replan_orientation_delta` (0.05 rad). When the final leg starts, it takes the latest target once, freezes it in `final_leg_anchor_frame` (`left_ankle_roll_link`) and ignores later targets. It then runs the final leg in `final_leg_steps` (10) steps. Each step is re-mapped to the current pelvis and starts `final_leg_settle_time` (0.5 s) after the previous one ends. The last step holds the press pose 1 s, then retreats along the final leg.
5. **Gripper fit.** Each final-leg step also corrects the aim by the gripper's depth fit (TF `*_dex1_base_link_estimate`, from `gripper_pose_estimator` on the Orin) minus the encoder gripper, and then aims 8 mm deeper (`LEAD_TOWARD_GOAL` in `planning.cpp`). Without a fit younger than `gripper_estimate_max_age` (0.5 s) and smaller than `gripper_estimate_max_error` (0.03 m), there is no correction and no extra depth. `check_reach` does not apply it.
6. **Done.** move_to_pose ends with `Target reached`, then the servo node with `Controller reached target`. This does not prove the button was pressed: `press_judge` decides that. `final_distance` is the closest the `*_ee` frame came to the goal during the call, not where it ended.

move_to_pose's own failures: `Planning failed: target out of reach: the arm ends X mm and Y deg from it` (the path misses the goal by more than `goal_position_tolerance` 5 mm or `goal_orientation_tolerance` 0.1 rad); `Replan failed` or `Final-leg step N failed`, which end the goal after `max_plan_failures` (3) in a row; `Arm is not following the trajectory` (a joint more than `max_tracking_error` 0.2 rad off for `tracking_error_timeout` 1 s, except within `near_target_distance` 0.02 m of the target, where a press looks the same).

The servo node resets track_anything whenever a goal ends: success, cancel, timeout or any failure. So the live target is gone by the time of a call, and the executor takes the press target from the last `current_goal` in the failure record.

### Poses and frames

- **Normal pose** (track_anything, `button_in_view`'s `pose_topic`): the centroid of the fitted plane, +X out of the panel toward the camera.
- **Upright goal** (`check_reach`'s `pose_topic`, in `foot_world`): same position, yaw only, +X into the panel. This is `current_goal` and the press pose.
- **Pre-grasp**: the upright goal moved back along its own −X by the planner's `approach_offset` (0.10 m, `visual_servo_planner.h`; `press_button`'s `approach_offset_m` repeats it). The path to the pre-grasp and every final-leg point must be solvable, or the plan fails as out of reach. Not checked in code: with the panel near, the pre-grasp comes close to the torso.
- **Press offset**: `arm_safety_distance_{x,y,z}` on move_to_pose, added in the target's own frame after the move into `pelvis` (`to_pelvis_frame_`). `set_press_offset` sets it; its contract says the frame and that a relaunch resets it. `check_reach` adds the offset itself, since ik_server keeps its launch offset (its contract's `notes`).
- **TF**: `foot_world` is published by `body_ekf`. `left_ee` and `right_ee` sit on the `*_wrist_yaw_link` at the end-effector `offset` in `g1.yaml`; move_to_pose publishes them. `camera_link` is fixed on `torso_link` (`d435_joint` in the `_ros.urdf`). `base_link`, the navigation base, is `sensor` (the lidar on the torso) moved 0.35 m down, with the lidar's orientation (bringup `nuc.launch.py`). `navigate_to` puts `base_link` on the goal. Not checked in code: `stance` measures `pelvis` 1 to 2 cm and about 2° plus the waist yaw away from it.

### Arm

- The profile in use (`active_config`) is `arms_with_waist`. Its `joint_limit_overrides` hold the three waist joints within ±0.0001 rad, so the torso cannot lean, and tighten the shoulder limits. Use them over the URDF's.
- IK is C++ (`HumanoidIK`, CasADi; damped least squares on the final leg). `check_reach` asks ik_server, which runs the same plan from the live joint state with the same tolerances, so its verdict matches the first plan of a press.
- Joints move at most `max_joint_vel` (0.5 rad/s); the final leg and the retreat at half that, 0.25 rad/s.

### Perception

- `/track_3d/is_tracking` true means a 2D mask in that frame. The 3D pose can still be missing: an object with fewer than 4 depth points, before or after outlier filtering, is dropped with only a log line.
- After a loss, `is_tracking` is false. After `tracking_timeout` (20 s) the tracker stops and does not ask the VLM again.
- How close the D435 sees depth is in `docs/troubleshooting.md` (Pressing).

### Navigation

- When `navigate_to` ends is in its contract's `notes`; walking's preconditions and a goal's storey in z are in `docs/troubleshooting.md` (Walking).
- Side and back steps are capped at `max_lateral_speed` and `max_backward_speed` (0.15 m/s), but `min_speed` (0.2 m/s) is higher and wins: the G1 does not step slower (`local_planner/config/unitree_g1.yaml`).

### Where to check these facts on the NUC

Under `~/eigen_vector_ws`. Which branch the NUC runs is not checked here; `docs/machines.md` owns it.

| What | Path |
|---|---|
| Arm planner's robot model | `install/g1_description/share/g1_description/assets/g1/g1_29dof_with_hand_rev_1_0.urdf`; pass its directory as the package directory for meshes |
| Model with `camera_link` | `g1_29dof_with_hand_rev_1_0_ros.urdf`, same directory |
| IK profiles, joint limits, end effectors | `install/g1_pilot/share/g1_pilot/config/g1.yaml` |
| Visual servo node, target filter, upright pose | `src/manipulation_stack/humanoid_arm_skills/humanoid_arm_skills/`: `humanoid_arm_visual_servoing.py`, `pose_filtering.py`, `utils.py` |
| move_to_pose, ik_server, planner | `src/manipulation_stack/g1_pilot/src/move_to_pose/`, `g1_pilot/src/ik_server.cpp`, `g1_pilot/src/core/arm_mp/` |
| Launch defaults | `src/bringup/launch/nuc.launch.py`, `humanoid_arm_skills/launch/humanoid_arm_visual_servoing.launch.py`, `g1_pilot/launch/move_to_pose.launch.py` |
| Navigation config | `src/navigation_stack/local_planner/config/unitree_g1.yaml` |

track_anything runs on the Orin and its VLM on the Thor. Sources: manipulation_stack `main` @ `f8d28b3`, track_anything `main` @ `4d1c755`, navigation_stack `main` @ `4cf9d87` (the same here as `jean/elevator-side`), and the bringup in eigen_vector `main` @ `dfabc71`. Check them again when those change.
