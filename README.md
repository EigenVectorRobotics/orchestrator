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
| `skill_registry/agent_skills/<skill>.md` | What an agent skill decides and how the tree uses it: `manipulation_recovery`, `press_judge`, `operator_confirm`. The model's prompts are in `mission_control/agent_prompts.py` |
| `skill_registry/agent_skills/manipulation_recovery.experience.md` | What the team has observed about presses (E-xxx): symptoms, conditions, causes, what helped, evidence |
| `skill_registry/agent_skills/navigation.experience.md` | The same for walking, localization and the processes navigation needs (N-xxx) |
| `skill_registry/trees/*.xml` | The tree library the planner reuses; each file's first comment says what the tree does and its parameters |
| `mission_control/README.md` | The agent bridge on the laptop: set it up, run it, answer `operator_confirm` |
| `docs/knowledge.md`, `docs/recorder.md`, `docs/skill-packages.md` | Designs: the knowledge store (claims), run recording, skill packages and graduation |

Facts that hold by construction go in the skill documents and the contracts; observations, with their dates and evidence, go in the experience files; procedures go in operations and troubleshooting, citing the entries they rest on.

## How it works

1. A task comes in on `/orchestrator/task`: a plain sentence, or `{"task": "...", "blind": true}`.
2. The planner sends the task, the skill catalog, the places and the live skills to the LLM. The LLM gives a BehaviorTree.CPP v4 XML tree.
3. The validator checks the tree. If there are errors, the planner sends them back to the LLM (3 attempts at most).
4. The executor validates the tree again, then ticks it at 10 Hz. It calls each skill from its contract: action, service, topic, or a closed question to the VLM on Thor.
5. The trace goes to `/orchestrator/trace` (JSON) and `/orchestrator/log` (`rcl_interfaces/Log`, for the Foxglove Log panel).

Blind mode (`/orchestrator/tree_blind`, or `"blind": true`): the tree cannot use skills that need the cloud. Plan it before the robot goes into the elevator.

## Add a skill

Write one YAML file in `skill_registry/contracts/`. No code is necessary if the skill is an action, service, topic or VLM question. See `cancel_nav.yaml` (topic), `navigate_to.yaml` and `press_button.yaml` (action), `set_profile.yaml` (service with an enum), `door_open.yaml` (topic watch) and `ask_vlm.yaml` (VLM). An input's `field` can index a sequence: `set_press_offset.yaml` sets `parameters.0.value.double_value` of an `rcl_interfaces/srv/SetParameters` request whose `fixed` lists the parameters; a service reply with per-item `successful` flags fails if any is false. A pose input takes a place (`{inside_freight_elevator_button_panel}`) or a literal `"x y yaw"` or `"x y z yaw"` in `map`; give z on the two-storey map, or the route planner takes the lowest storey. A transport checks the stance before the first press: `in_view` seeds track_anything with the button's name, then checks that the first 2D box and 3D pose after the seed exist and the box is away from the image edges (`button_in_view.yaml`). A watch compares with a tree input through `equals_input`, as in `at_floor.yaml` (the floor state of `slam_elevatorlio`). A condition with `record: true` has every answer recorded in the history, a pass included, so the agent sees it (`check_reach.yaml`). `orchestrator/config/knowledge_seed.yaml` holds the team's experience about stances as claims: load it with `ros2 run orchestrator knowledge seed <file>`.

## Agent skills

An agent skill hands one decision of the tree to a model or to the operator. Its contract has `transport: agent` and names a document in `skill_registry/agent_skills/`; the executor reads it at each call, so each agent skill needs one. `manipulation_recovery` is the first one: after `press_button` fails it finds out why (mainly: was the button out of reach?) and decides reposition, switch_arm, offset_retry, retry, ask_help or abort. Before the first press, when a look fails (`button_in_view` or `check_reach`), it checks the stance and decides continue, reposition or abort. `press_judge` is the second: after every press it looks at the camera frame at the fingertip's closest point to the button and decides pressed or not_pressed ("Checking a press"). See `orchestrator/test_trees/freight_elevator_floor1_press_agentic.xml` and, on the robot, `skill_registry/trees/freight_elevator_go_to_place.xml`.

- **Evidence.** Every finished action call is recorded in the tree's history; a failure keeps the action's feedback over time. Both go to the trace as `record` events, and to the agent call as its `failure` and `history`.
- **No tools.** The bridge's model gets the evidence as text and the frames as images, and answers with one decision. It never publishes and never moves the robot. The executor checks the decision and refuses one outside the limits.
- **Rules before the model.** The executor answers some calls itself, with a reason that starts `rule:`. `rule_offset` corrects a near miss after a reached goal. `rule_stance` sends the robot back onto its target stance. `rule_reach` steps by the IK miss of a failed `check_reach`. The limits are constants in `orchestrator/agent.py`.
- **Branching.** `decision_is` compares a blackboard value (`value="{decision}" equals="reposition"`). `abort` fails the whole tree at once with the agent's reason. After `ask_help`, or after `max_rounds` diagnoses, the agent skill fails without a call.
- **Who answers.** The agent bridge on the laptop (`mission_control/agent_bridge.py`) answers every call. The executor pushes each call with its evidence and frames over one TCP link and waits up to the contract timeout. The bridge asks the model (Claude Agent SDK) for `press_judge` and `manipulation_recovery`. It asks the operator in its terminal for `operator_confirm`. `agent_backend:=none` runs without an agent: agent skills then fail.
- **The request is whole.** The executor takes the failure, the history and the press from the tools with `whole`, so a 60 s press (120 feedback samples, over 40 kB) reaches the bridge.
- **Recorded.** Each call also goes out on `/orchestrator/agent/request` (opened, then closed) and each reply on `/orchestrator/agent/reply`. The run recorder and the Run Viewer read them.
- **Executor parameters.** `agent_backend` (`push` or `none`), `agent_push_host`, `agent_push_port`, `agent_skills_dir`, `knowledge_dir`.

### Standard recoveries

From `manipulation_recovery.experience.md`; the trees, the executor's rules and the bridge's prompts carry them out:

| Case | Signs | Recovery |
|---|---|---|
| A walk routes but never moves | `/free_paths` empty, one-pose `/path`, latched points in `/terrain_map` close ahead | The walks into and out of the cabin (subtrees `walk_in`, `walk_out`): on failure `say`, `clear_terrain_map`, 2 s, and walk again, three times at most |
| The press reached its goal but the finger missed by millimetres (rim, beside, on the face but unlit) | `press_judge` `not_pressed`; `residual_m` a few mm to about 20 mm | `offset_retry`: the new offset is the offset in force minus the residual (on floor buttons push the depth further); the tree runs `set_press_offset` and presses again. The executor's `rule_offset` does this on the first miss of 5 to 50 mm in the button plane, or over 3 mm short; after that the model decides |
| The robot stopped too close; the button is below the frame | `press_button` times out with `distance_to_goal` inf throughout; `ground_server` logs `0 box(es)` | `reposition` back: before the first press, the back-off stance 0.15 m behind the robot |
| The robot is too far; the arm stops short | the closest point tens of mm short though the action succeeded | Before the press, `check_reach` fails and `rule_reach` steps by the IK miss. After a press, `reposition` closer |

**A short reposition walks out first.** A goal close to the robot ends at once without a step (E-016). When a reposition goal is under 0.10 m from the robot, the executor sets `walk_in`, and the look walks to `approach`, 0.15 m behind the goal, then onto it. The executor refuses a reposition more than 0.15 m past the base goal, 0.60 m behind it or 0.25 m to the side.

### Press offsets per panel

`set_press_offset x y z` sets move_to_pose's `arm_safety_distance_{x,y,z}` through `/move_to_pose_server/set_parameters` (x along the button normal, positive deeper; y left; z up; metres). They live only in the running node. Each press slot starts from its panel's base offset (`press_base_offset_{x,y,z}` in `subtrees/elevator.xml`), so an offset never carries over from one panel to the other: 0 at the call panel, +0.003 on each axis inside. An `offset_retry` sets the offset for the next press. The offset in force is the last `set_press_offset` in the tree's history.

### The agent says what it is doing, all the time

The robot speaks the agent's process out loud: what it is checking, and what was decided. Each message is one short English sentence, like thinking aloud, not the full reasoning.

- **Where:** a `std_msgs/String` on `/speak`. unitree_interface subscribes `~/tts`, and its launch file remaps `/unitree_interface/tts` to `/speak`; it hands each message to the Unitree TTS (`TtsMaker`) at once, with no queue.
- **How:** the executor says the sentences of `PHASE_SPEECH` and `DECISION_SPEECH` in `agent.py`, and one sentence for each rule that corrects the press or the stance.
- **When:** when a call opens, when a rule corrects the press or the stance, and when a decision comes.
- **Examples:** "The press did not work. Checking why." "The press just missed the button. Correcting my aim and pressing again." "Moving to a better spot."

## Checking a press

**`press_judge` (agent in the loop) is the check the trees use now.** After `press_button` succeeds, the executor waits until the press's three frames are chosen (below), saves them, and opens an agent call with their paths, the phase times, the deepest point along the button normal, the sideways distance and the signed residual (fingertip minus goal at the closest point, in the goal frame: x into the panel, y left, z up). The bridge's model looks at the `press` frame (the first after the fingertip's closest point), the `release` frame and the `baseline`, and decides `pressed` (success) or `not_pressed`. A `not_pressed` is recorded as the failed check of that press, so `manipulation_recovery` sees the press as `checked_call` with its feedback. The skill document is `skill_registry/agent_skills/press_judge.md`. The requested button's own light decides on both panels: the blue ring on floor buttons, the call button's light on the call panel (`JUDGE_SYSTEM` in `mission_control/agent_prompts.py`). `press_button` succeeding does not prove a press: once the fingertip stopped 67 mm short and the action still reported `Controller reached target` (E-018).

**The closest point is found after the pre-grasp.** Until the end effector reaches the pre-grasp, the phase tracker counts only samples within `lateral_tolerance` (30 mm) of the button axis. Counting the arm's swing up from its rest pose, which passes the goal's depth about 20 cm below the button, ended the press there and reported misses of 19 to 23 cm sideways after good presses (E-014).

The rest of this section is how the frames are chosen.

`press_button` keeps three camera frames (`interface.capture` in its contract; `orchestrator/press_capture.py`): a baseline when the end effector reaches the pre-grasp, the first frame at least 5 ms after the deepest point of the push, and the first frame at least 0.5 s after the retreat began. The phases come from TF `right_ee` or `left_ee`, sampled in the goal's frame every 10 ms and used once per new TF stamp (the arm TF arrives at 10 to 20 Hz), against the action feedback's `current_goal`. Each frame is also cropped to the area around the button: track_anything's box for that frame (`/track_3d/detections_2d`, the target id from `/track_3d/selected_target_3d`; the box with the frame's stamp, or the latest before it within 1.5 s), grown 2.5 times about its centre (`crop_scale`). A frame without a box of its own, as when the hand covers the button, uses the baseline's box; with no box at all, only whole frames are kept. The frames and their crops (`*_crop.jpg`) are saved under `capture_dir` (executor parameter, default `~/.ros/orchestrator_captures`).

- The camera runs at 6 fps (1280x720x6, raw RGB8 on the Orin), so a chosen frame can be up to about 0.17 s after its moment.
- Frame times are the camera's header stamps and pose times the NUC's: the Orin's and the NUC's clocks must agree.
- **The VLM on the Thor.** The executor's VLM conditions post to `vlm_base_url` (default `http://vector-thor:8090/v1`), but the Thor now runs only the Cosmos daemon and `ground_server`. Start `ros2 run orchestrator cosmos_vlm_server --port 8090` on the Thor, in the environment that runs `ground_server`: it answers with the Cosmos daemon, one request at a time, and waits while `ground_server` uses it. `vlm_timeout_s` (executor, default 40 s) covers that wait.

## Knowledge store

What the robot learns is kept as claims in `knowledge_dir` (executor parameter, default `~/orch_knowledge`): `claims.yaml`, and `log.jsonl` with every change. A claim is an observation, a belief or a fact (`docs/knowledge.md`). Claims come from the operator and the seed file; no model reads or writes them now, since the bridge builds its own prompts. Only the operator makes a fact, at a terminal:

```bash
ros2 run orchestrator knowledge list --ready                  # beliefs that can become facts
ros2 run orchestrator knowledge show C-0007
ros2 run orchestrator knowledge note "The call button lights when the call registers." --by operator --evidence frame:release --run <run_id> --about place=freight_elevator_call_panel --key lights_when_pressed --value true
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
ros2 run orchestrator send_tree my_tree.xml --blind            # --blind: validate and run it with no cloud link
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
- The inside press starts at the base offset (+3, +3, +3) mm; one press lit only at (+12, +12.6, +3.2) mm (E-012). Start there once a second press confirms it.
- The `ekf_hold` subtree resets the body EKF once per stance, in `look_here` after the mode change and before `check_reach` (`reset_ekf`, `/body_ekf/reset`). Every press, a `continue` included, follows a passed `look_here` at its stance. The skill ends when body_ekf publishes its torso pose again, which it does only once it has recalibrated.
- `press_judge` needs the agent bridge's model (a cloud call) to look at frames.

- `places.yaml` has zeros. Measure the poses on the prior map.
- Timeouts in the contracts are guesses. All skills are `wip` or `untested`.
- The service names under `/unitree_interface/` and `/slam_elevatorlio/` come from the `~/` names in the code. Check them on the robot.
- `ground`, `set_profile`, `set_hand`, `save_map` and `load_map` need their interface packages on the NUC.
- The executor reads the raw camera topic (`image_topic`, default `/camera/camera/color/image_raw`), because the Orin has no compressed image transport installed, and encodes the frames it keeps to JPEG. That brings the raw stream to the NUC: about 2.8 MB a frame, about 17 MB/s at 6 fps.
- `thor_up` checks the grounding service and the VLM endpoint together, and preflight now stops when the Thor is not up. A Thor with the grounding up and the VLM endpoint down (the endpoint is not needed by `freight_elevator_go_to_place`) stops the task too. Split the check, or check the grounding only.
- `planner_cli answer --use-tree` sets a library tree's parameters (`--param`) but cannot swap its subtrees; that needs `--tree FILE`.
- On hold: in blind mode, every action skill must have a fallback.
