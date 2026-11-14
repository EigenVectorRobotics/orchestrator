# Orchestrator

Agentic planning for the G1. An LLM writes a behavior tree, a validator checks it, and the NUC runs it. Every step goes to a trace topic.

| Package | Holds |
|---|---|
| `skill_registry` | Skill contracts (`contracts/*.yaml`), the tree validator, live-skill discovery, the tree library (`trees/`, curated example trees the planner can reuse) |
| `orchestrator` | Tree executor, ROS skill runner, planner, trace, fake robot; `test_trees/`, trees for tests and runs (not shown to the planner) |

## Documents

| Document | Read it for |
|---|---|
| `docs/operations.md` | How to run it on the robot: deploy on the NUC, start the orchestrator, run a task through the planner, run one tree, bring a machine up by hand, stop, watch a run |
| `docs/troubleshooting.md` | Symptom, checks, fix: machines and processes, localization, walking, pressing, the orchestrator |
| `docs/deployment.md` | A snapshot of what runs where: each machine's access, workspace, repositories and branches, local changes, and files that exist only on a machine |
| `skill_registry/agent_skills/<skill>.md` | An agent skill's instructions (its system prompt): `manipulation_recovery`, `press_judge`, `operator_confirm` |
| `skill_registry/agent_skills/manipulation_recovery.experience.md` | What the team has observed about presses (E-xxx): symptoms, conditions, causes, what helped, evidence. The recovery agent reads it |
| `skill_registry/agent_skills/navigation.experience.md` | The same for walking, localization and the processes navigation needs (N-xxx) |
| `skill_registry/trees/*.xml` | The tree library the planner reuses; each file's first comment says what the tree does and its parameters |
| `docs/knowledge.md`, `docs/recorder.md`, `docs/skill-packages.md`, `docs/skill-structure.md` | Designs: the knowledge store (claims), run recording, skill packages and graduation, the structure of a skill |

Facts that hold by construction go in the skill documents and the contracts; observations, with their dates and evidence, go in the experience files; procedures go in operations and troubleshooting, citing the entries they rest on.

## How it works

1. A task comes in on `/orchestrator/task`: a plain sentence, or `{"task": "...", "blind": true}`.
2. The planner sends the task, the skill catalog, the places and the live skills to the LLM. The LLM gives a BehaviorTree.CPP v4 XML tree.
3. The validator checks the tree. If there are errors, the planner sends them back to the LLM (3 attempts at most).
4. The executor validates the tree again, then ticks it at 10 Hz. It calls each skill from its contract: action, service, topic, or a closed question to the VLM on Thor.
5. The trace goes to `/orchestrator/trace` (JSON) and `/orchestrator/log` (`rcl_interfaces/Log`, for the Foxglove Log panel).

Blind mode (`/orchestrator/tree_blind`, or `"blind": true`): the tree cannot use skills that need the cloud. Plan it before the robot goes into the elevator.

## Add a skill

Write one YAML file in `skill_registry/contracts/`. No code is necessary if the skill is an action, service, topic or VLM question. See `cancel_nav.yaml` (topic), `navigate_to.yaml` and `press_button.yaml` (action), `set_profile.yaml` (service with an enum), `door_open.yaml` (topic watch) and `ask_vlm.yaml` (VLM). An input's `field` can index a sequence: `set_press_offset.yaml` sets `parameters.0.value.double_value` of an `rcl_interfaces/srv/SetParameters` request whose `fixed` lists the parameters; a service reply with per-item `successful` flags fails if any is false. A pose input takes a place (`{inside_freight_elevator_button_panel}`) or a literal `"x y yaw"` or `"x y z yaw"` in `map`; give z on the two-storey map, or the route planner takes the lowest storey. One read-only transport runs inside the executor: `press_tip` judges where the end effector got to in the last press (`press_reached.yaml`). `in_view` checks the stance before the first press, with no VLM call of its own: it seeds track_anything with the button, then checks that the first 2D box after the seed is away from the image edges and that a 3D pose came, and picks the arm from where the button sits in `pelvis` (`button_in_view.yaml`). A watch compares with a tree input through `equals_input`, as in `elevator_floor_is.yaml` (the IMU floor counter of `elevator_states`). A condition with `record: true` has its failure recorded for the agent even when it checks no action. `orchestrator/config/knowledge_seed.yaml` holds the team's experience about stances as claims: load it with `ros2 run orchestrator knowledge seed <file>`.

## Agent skills

An agent skill is an LLM with read-only tools that decides how a tree recovers. Its contract has `transport: agent` and names a document in `skill_registry/agent_skills/`, which is the agent's system prompt. `manipulation_recovery` is the first one: after `press_button` fails it finds out why (mainly: was the button out of reach?) and decides reposition, switch_arm, offset_retry, retry, ask_help or abort; before the first press, when `button_in_view` fails, it decides continue, reposition or abort. `press_judge` is the second: after every press it looks at the camera frame at the fingertip's closest point to the button and decides pressed or not_pressed ("Checking a press"). See `skill_registry/trees/freight_elevator_go_to_place.xml` and its subtrees in `skill_registry/trees/subtrees/`.

- **Evidence.** Every finished action call is recorded in the tree's history; a failure keeps the action's feedback over time. Both go to the trace as `record` events, and to the agent through `get_failure` and `get_history`.
- **Tools.** `read_file`, `ros_read` (TF, one message, the topic list), `get_image`, `run_python` (numpy, scipy, pinocchio, coal), `decide`. Nothing publishes. `run_python` runs under an audit hook (no ROS, network, processes, or writes outside its scratch directory): a guard against mistakes, not a security boundary, since the NUC allows no user namespaces.
- **Branching.** `decision_is` compares a blackboard value (`value="{decision}" equals="reposition"`). `abort` fails the whole tree at once with the agent's reason. After `ask_help`, or after `max_rounds` diagnoses, the agent skill fails without a call.
- **Who answers.** By default (`agent_backend:=external`) a session outside the executor, e.g. Claude Code over ssh: the executor publishes each call on `/orchestrator/agent/request` and waits up to the contract timeout (600 s); the session gathers evidence and answers with `agent_cli`, which publishes only the decision, and only with `--send`. Other backends run inside the executor: `openai_compatible` with `agent_base_url` and `agent_model` (e.g. the Thor's local model), `scripted` with `agent_script` (tests), `none`.
- **agent_cli.** `wait`, `show [--full]`, `tf TARGET SOURCE`, `topic TOPIC`, `topics`, `image camera|overlay OUT`, `python FILE` (the sandbox), `say TEXT`, `decide DECISION --reason R [--dx --dy --dyaw] [--ox --oy --oz] [--left-arm|--right-arm] [--send]`. Run it in the executor's ROS environment. For a `press_judge` call, `show` adds a `press:` line (arm, deepest point along the button normal, sideways distance, signed `residual_m`) and the paths of the saved frames on the NUC; copy them with `scp` to look at them.
- **The request is whole.** The external backend takes the failure, the history and the press from the tools with `whole`, so a 60 s press (120 feedback samples, over 40 kB) reaches the session. A model inside the executor still gets tool text cut at 40 kB.
- **Executor parameters.** `agent_backend`, `agent_read_roots`, `agent_scratch_dir`, `agent_python_timeout_s`, `agent_max_turns`, and the backend ones above.

### Standard recoveries

From `manipulation_recovery.experience.md` (E-014 to E-020); the trees and the skill document carry them out:

| Case | Signs | Recovery |
|---|---|---|
| A walk routes but never moves | `/free_paths` empty, one-pose `/path`, latched points in `/terrain_map` close ahead | Every walk in the trees is `navigate_to`; on failure `say`, `clear_terrain_map`, 2 s, and walk again, three times at most (subtrees `walk_*`) |
| The press reached its goal but the finger missed by millimetres (rim, beside, on the face but unlit) | `press_judge` `not_pressed`; `residual_m` a few mm to about 20 mm | `manipulation_recovery` `offset_retry`: the new offset is the offset in force minus the residual (on floor buttons push the depth further); the tree runs `set_press_offset` and presses again |
| The robot stopped too close; the button is below the frame | `press_button` times out with `distance_to_goal` inf throughout; `ground_server` logs `0 box(es)` | `reposition` back, to a stance at least 0.25 m from where the robot stands |
| The robot is too far; the arm stops short | the closest point tens of mm short though the action succeeded | `reposition` closer, again at least 0.25 m away; when that is not possible, walk out and in again |

**A reposition must be at least about 0.25 m from where the robot stands** (`/odom_base_link`, counting the sideways part): goals 0.148 and 0.18 m away ended at once without a step (E-016). When the stance needed is closer than that, the robot has to walk away and come back: walk to a pose about 0.35 m behind the place, then to the place.

### Press offsets per panel

`set_press_offset x y z` sets move_to_pose's `arm_safety_distance_{x,y,z}` through `/move_to_pose_server/set_parameters` (x along the button normal, positive deeper; y left; z up; metres). They live only in the running node. The trees set them before every first press, so an offset never carries over from one panel to the other: 0 at the call panel; (+0.005, +0.0075, +0.0034) inside, right arm, floor buttons (what lit and what did not: E-011, E-012, E-020). The offset in force is the last `set_press_offset` in the tree's history.

### The agent says what it is doing, all the time

Whatever is the agent (a Claude Code session or a model in the executor), it speaks its process out loud the whole time it works: what it is checking, what it found, what problem it hit, what it is trying next. Each message is one short English sentence, like thinking aloud, not the full reasoning.

- **Where:** a `std_msgs/String` on `/speak`. unitree_interface subscribes `~/tts`, and its launch file remaps `/unitree_interface/tts` to `/speak`; it hands each message to the Unitree TTS (`TtsMaker`) at once, with no queue.
- **How:** in a session, `agent_cli say "Checking whether the arm can reach the button."`; inside the executor, the agent's `say` tool. Both refuse empty text and more than 200 characters; the tool also waits 3 s between messages, since a new message does not wait for the last one to finish.
- **When:** at the start of each call; at each step of the diagnosis (reading the failure, checking reach, looking at the frame); whenever something fails or surprises it; before each decision it proposes or sends, and while it waits for the operator's go.
- **Examples:** "The press stopped 13 centimetres short; checking reach." "The right arm cannot reach; trying the left." "The button is out of view; I will step back." "I propose to step back 20 centimetres; waiting for your go."

## Checking a press

**`press_judge` (agent in the loop) is the check the trees use now.** After `press_button` succeeds, the executor waits until the press's three frames are chosen (below), saves them, and opens an agent call with their paths, the phase times, the deepest point along the button normal, the sideways distance and the signed residual (fingertip minus goal at the closest point, in the goal frame: x into the panel, y left, z up). The agent (a session, through `agent_cli`) looks at the `press` frame, the first after the fingertip's closest point, and the `release` frame, and decides `pressed` (success) or `not_pressed`. A `not_pressed` is recorded as the failed check of that press, so `manipulation_recovery` sees the press as `checked_call` with its feedback. The skill document is `skill_registry/agent_skills/press_judge.md`. What decided the presses in the trial: on floor buttons the blue ring, lit at once and still lit in the release frame; on the call panel, which does not light, where the finger sits, often clearest in the release frame as the finger backs off (E-019). `press_button` succeeding does not prove a press: once the fingertip stopped 67 mm short and the action still reported `Controller reached target` (E-018).

**The closest point is found after the pre-grasp.** Until the end effector reaches the pre-grasp, the phase tracker counts only samples within `lateral_tolerance` (30 mm) of the button axis. Counting the arm's swing up from its rest pose, which passes the goal's depth about 20 cm below the button, ended the press there and made `press_reached` report misses of 19 to 23 cm sideways after good presses (E-014).

The rest of this section is how the frames are chosen, and the two older checks, `button_lit` and `press_reached`, which the trees no longer use.

`press_button` keeps three camera frames (`interface.capture` in its contract; `orchestrator/press_capture.py`): a baseline when the end effector reaches the pre-grasp, the first frame at least 5 ms after the deepest point of the push, and the first frame at least 0.5 s after the retreat began. The phases come from TF `right_ee` or `left_ee`, sampled in the goal's frame every 10 ms and used once per new TF stamp (the arm TF arrives at 10 to 20 Hz), against the action feedback's `current_goal`. `button_lit` then asks the VLM about the press frame and the release frame, each beside the baseline in one image; lit in either is success. Each frame is cropped to the area around the button: track_anything's box for that frame (`/track_3d/detections_2d`, the target id from `/track_3d/selected_target_3d`; the box with the frame's stamp, or the latest before it within 1.5 s), grown 2.5 times about its centre (`crop_scale`). A frame without a box of its own, as when the hand covers the button, uses the baseline's box; with no box at all, whole frames are asked. The frames and their crops (`*_crop.jpg`) are saved under `capture_dir` (executor parameter, default `~/.ros/orchestrator_captures`).

- The camera runs at 6 fps (1280x720x6, raw RGB8 on the Orin), so a chosen frame can be up to about 0.17 s after its moment.
- **Where a button does not light**, use `press_reached` instead of `button_lit`. It reads the same capture: the deepest point of the push along the goal's +X, and the sideways distance from the goal there. It succeeds when the depth is at least `min_depth_m` (-0.012) and the sideways distance at most `max_lateral_m` (0.008), from the contract's `limits`. The trace gives both numbers. The poses are forward kinematics in `foot_world`, so a drifting body EKF moves the goal, not the measure: a correct press check can still follow a press that went to the wrong place.
- Frame times are the camera's header stamps and pose times the NUC's: the Orin's and the NUC's clocks must agree.
- **The VLM on the Thor.** The executor's VLM conditions post to `vlm_base_url` (default `http://vector-thor:8090/v1`), but the Thor now runs only the Cosmos daemon and `ground_server`. Start `ros2 run orchestrator cosmos_vlm_server --port 8090` on the Thor, in the environment that runs `ground_server`: it answers with the Cosmos daemon, one request at a time, and waits while `ground_server` uses it. `vlm_timeout_s` (executor, default 40 s) covers that wait.

## Knowledge store

What the robot learns is kept as claims in `knowledge_dir` (executor parameter, default `~/orch_knowledge`): `claims.yaml`, and `log.jsonl` with every change. A claim is an observation, a belief or a fact (`docs/knowledge.md`). An agent skill adds observations with its `note` tool and sees the facts and beliefs in its prompt. A session that answers agent calls from outside uses the CLI, with `--by agent:<skill>`. Only the operator makes a fact, at a terminal:

```bash
ros2 run orchestrator knowledge list --ready                  # beliefs that can become facts
ros2 run orchestrator knowledge show C-0007
ros2 run orchestrator knowledge note "The call panel does not light." --by agent:press_judge --evidence frame:press --run <run_id> --about place=freight_elevator_call_panel --key lights_when_pressed --value false
ros2 run orchestrator knowledge approve C-0007 [--text "the exact text"]
```

## Run on a laptop, with no robot

```bash
ros2 launch orchestrator orchestrator.launch.py fake:=true planner_backend:=scripted
ros2 topic pub --once /orchestrator/task std_msgs/msg/String "{data: 'Go to the elevator and call it'}"
ros2 topic echo /orchestrator/trace --field data
```

All nodes load `orchestrator/config/orchestrator.yaml` (`params_file:=` for another). A places file is this package's format (`name: {x, y, yaw}`) or the team's map format (`places:` with `yaw_deg`, e.g. `~/maps/two_floor_clean_cabin2.places.yaml` on the NUC), whose names become snake-case keys: "freight elevator call panel" is `{freight_elevator_call_panel}`. On the robot, see `docs/operations.md`. `contracts_directory`, `places_file` and `vlm_base_url` are launch arguments, passed to every node.

`fake_robot` has the same topics, services, navigation and arm actions, and VLM server as the robot. The two actions need `navigation_interfaces` (from `navigation_stack`) and `humanoid_manipulation_interfaces` built in the workspace; without one, its skill fails with no server. Parameters: `press_success:=false`, `press_fail_count:=1` (the first presses stall 0.13 m short, as out of reach, with feedback), `nav_blocked:=true` (every route turns blocked until cancelled; navigate_to fails after 10 s), `vlm_answers:=door=closed`, `nav_time_s:=5.0`, `vlm_port`.

Run one tree with no planner:

```bash
ros2 run orchestrator send_tree orchestrator/test_trees/call_elevator.xml --blind
ros2 run skill_registry validate_tree my_tree.xml --blind      # check only
```

## Planner backends

| `planner_backend` | Needs |
|---|---|
| `claude` (default, model `claude-opus-5`) | `pip install anthropic`, and `ANTHROPIC_API_KEY` or an `ant auth login` profile |
| `openai_compatible` | `base_url`, `model`, and the key in the env var that `api_key_env` names. For GPT, Gemini's OpenAI endpoint, vLLM |
| `scripted` | Nothing. It replies with `scripted_tree` |
| `external` | A Claude Code session that answers with `planner_cli` (`wait`, `show`, `answer --use-tree ID` or `--tree FILE`); the planner waits up to `external_timeout_s` (600 s) per call |

## Not done yet

- Arrivals are poor: the robot has stopped up to 24 cm past a place, and an arrival 8 cm or more past puts the button out of view (E-001, E-015). Set `path_follower` `slow_down_distance_threshold` 0.7 again (a relaunch resets it to 0.4) and compare, or move the places back by the mean overshoot.
- A reposition 0.18 m away ended at once without a step; why is not known (E-016).
- The inside press starts at the offset (+5.0, +7.5, +3.4) mm; one press lit only at (+12, +12.6, +3.2) mm. Start there once a second press confirms it.
- The `ekf_hold` subtree resets the body EKF once per stance, in `look_here` after the mode change and before `check_reach` (`reset_ekf`, `/body_ekf/reset`). Every press, a `continue` included, follows a passed `look_here` at its stance. The skill ends when body_ekf publishes its torso pose again, which it does only once it has recalibrated.
- `press_judge` needs a person or a session to look at frames; the VLM could judge the button lights alone (`button_lit`).

- `places.yaml` has zeros. Measure the poses on the prior map.
- Timeouts in the contracts are guesses. All skills are `wip` or `untested`.
- The service names under `/unitree_interface/` and `/slam_elevatorlio/` come from the `~/` names in the code. Check them on the robot.
- `ground`, `set_profile`, `set_hand`, `save_map` and `load_map` need their interface packages on the NUC.
- `reset_tracker` ends only when `/track_3d/is_tracking` changes. If it is already false, it times out.
- `body_ekf` recalibrates in place through `/body_ekf/reset` (`std_srvs/Trigger`): call it at the panel right before a press (docs/troubleshooting.md, "Pressing").
- A failed `press_reached` does not reach `manipulation_recovery`: `get_failure` records only failed actions, failed `button_lit` checks and `press_judge` `not_pressed`, so the agent sees `failure: none recorded`. The trees use `press_judge` instead.
- The executor reads the raw camera topic (`image_topic`, default `/camera/camera/color/image_raw`), because the Orin has no compressed image transport installed, and encodes the frames it keeps to JPEG. That brings the raw stream to the NUC: about 2.8 MB a frame, about 17 MB/s at 6 fps.
- `thor_up` checks the grounding service and the VLM endpoint together, and preflight now stops when the Thor is not up. A Thor with the grounding up and the VLM endpoint down (the endpoint is not needed by `freight_elevator_go_to_place`) stops the task too. Split the check, or check the grounding only.
- `planner_cli answer --use-tree` cannot set a library tree's parameters or swap its subtrees; any ride other than the default needs `--tree FILE`.
- On hold: in blind mode, every action skill must have a fallback.
