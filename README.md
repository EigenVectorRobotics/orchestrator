# Orchestrator

Agentic planning for the G1. An LLM writes a behavior tree, a validator checks it, and the NUC runs it. Every step goes to a trace topic.

| Package | Holds |
|---|---|
| `skill_registry` | Skill contracts (`contracts/*.yaml`), the tree validator, live-skill discovery, example trees |
| `orchestrator` | Tree executor, ROS skill runner, planner, trace, fake robot |

## How it works

1. A task comes in on `/orchestrator/task`: a plain sentence, or `{"task": "...", "blind": true}`.
2. The planner sends the task, the skill catalog, the places and the live skills to the LLM. The LLM gives a BehaviorTree.CPP v4 XML tree.
3. The validator checks the tree. If there are errors, the planner sends them back to the LLM (3 attempts at most).
4. The executor validates the tree again, then ticks it at 10 Hz. It calls each skill from its contract: action, service, topic, or a closed question to the VLM on Thor.
5. The trace goes to `/orchestrator/trace` (JSON) and `/orchestrator/log` (`rcl_interfaces/Log`, for the Foxglove Log panel).

Blind mode (`/orchestrator/tree_blind`, or `"blind": true`): the tree cannot use skills that need the cloud. Plan it before the robot goes into the elevator.

## Add a skill

Write one YAML file in `skill_registry/contracts/`. No code is necessary if the skill is an action, service, topic or VLM question. See `cancel_nav.yaml` (topic), `navigate_to.yaml` and `press_button.yaml` (action), `set_profile.yaml` (service with an enum), and `door_open.yaml` (VLM). Two read-only transports run inside the executor: `graph` checks that each listed topic has at most its allowed publishers, not counting the executor's own (`no_stale_publishers.yaml`), and `press_tip` judges where the end effector got to in the last press (`press_reached.yaml`).

## Agent skills

An agent skill is an LLM with read-only tools that decides how a tree recovers. Its contract has `transport: agent` and names a document in `skill_registry/agent_skills/`, which is the agent's system prompt. `manipulation_recovery` is the first one: after `press_button` fails it finds out why (mainly: was the button out of reach?) and decides reposition, switch_arm, retry, ask_help or abort; after a reposition walk it checks that the button is in view. See `skill_registry/trees/call_elevator_agentic.xml`.

- **Evidence.** Every finished action call is recorded in the tree's history; a failure keeps the action's feedback over time. Both go to the trace as `record` events, and to the agent through `get_failure` and `get_history`.
- **Tools.** `read_file`, `ros_read` (TF, one message, the topic list), `get_image`, `run_python` (numpy, scipy, pinocchio, coal), `decide`. Nothing publishes. `run_python` runs under an audit hook (no ROS, network, processes, or writes outside its scratch directory): a guard against mistakes, not a security boundary, since the NUC allows no user namespaces.
- **Branching.** `decision_is` compares a blackboard value (`value="{decision}" equals="reposition"`). `abort` fails the whole tree at once with the agent's reason. After `ask_help`, or after `max_rounds` diagnoses, the agent skill fails without a call.
- **Who answers.** By default (`agent_backend:=external`) a session outside the executor, e.g. Claude Code over ssh: the executor publishes each call on `/orchestrator/agent/request` and waits up to the contract timeout (600 s); the session gathers evidence and answers with `agent_cli`, which publishes only the decision, and only with `--send`. Other backends run inside the executor: `openai_compatible` with `agent_base_url` and `agent_model` (e.g. the Thor's local model), `scripted` with `agent_script` (tests), `none`.
- **agent_cli.** `wait`, `show [--full]`, `tf TARGET SOURCE`, `topic TOPIC`, `topics`, `image camera|overlay OUT`, `python FILE` (the sandbox), `decide DECISION --reason R [--dx --dy --dyaw] [--left-arm|--right-arm] [--send]`. Run it in the executor's ROS environment.
- **Executor parameters.** `agent_backend`, `agent_read_roots`, `agent_scratch_dir`, `agent_python_timeout_s`, `agent_max_turns`, and the backend ones above.

### The agent says what it is doing, all the time

Whatever is the agent (a Claude Code session or a model in the executor), it speaks its process out loud the whole time it works: what it is checking, what it found, what problem it hit, what it is trying next. Each message is one short English sentence, like thinking aloud, not the full reasoning.

- **Where:** a `std_msgs/String` on `/speak`. unitree_interface subscribes `~/tts`, and its launch file remaps `/unitree_interface/tts` to `/speak`; it hands each message to the Unitree TTS (`TtsMaker`) at once, with no queue.
- **How:** in a session, `agent_cli say "Checking whether the arm can reach the button."`; inside the executor, the agent's `say` tool. Both refuse empty text and more than 200 characters; the tool also waits 3 s between messages, since a new message does not wait for the last one to finish.
- **When:** at the start of each call; at each step of the diagnosis (reading the failure, checking reach, looking at the frame); whenever something fails or surprises it; before each decision it proposes or sends, and while it waits for the operator's go.
- **Examples:** "The press stopped 13 centimetres short; checking reach." "The right arm cannot reach; trying the left." "The button is out of view; I will step back." "I propose to step back 20 centimetres; waiting for your go."

## Checking a press

`press_button` keeps three camera frames (`interface.capture` in its contract; `orchestrator/press_capture.py`): a baseline when the end effector reaches the pre-grasp, the first frame at least 5 ms after the deepest point of the push, and the first frame at least 0.5 s after the retreat began. The phases come from `/right_ee_pose` or `/left_ee_pose` (100 Hz, `foot_world`) against the action feedback's `current_goal`. `button_lit` then asks the VLM about the press frame and the release frame, each beside the baseline in one image; lit in either is success. Each frame is cropped to the area around the button: track_anything's box for that frame (`/track_3d/detections_2d`, the target id from `/track_3d/selected_target_3d`; the box with the frame's stamp, or the latest before it within 1.5 s), grown 2.5 times about its centre (`crop_scale`). A frame without a box of its own, as when the hand covers the button, uses the baseline's box; with no box at all, whole frames are asked. The frames and their crops (`*_crop.jpg`) are saved under `capture_dir` (executor parameter, default `~/.ros/orchestrator_captures`).

- The camera runs at 6 fps (1280x720x6, raw RGB8 on the Orin), so a chosen frame can be up to about 0.17 s after its moment.
- **Where the button does not light** (the freight elevator call panel), use `press_reached` instead of `button_lit`. It reads the same capture: the deepest point of the push along the goal's +X, and the sideways distance from the goal there. It succeeds when the depth is at least `min_depth_m` (-0.012) and the sideways distance at most `max_lateral_m` (0.008), from the contract's `limits`. The trace gives both numbers. The poses are forward kinematics in `foot_world`, so a drifting body EKF moves the goal, not the measure: a correct press check can still follow a press that went to the wrong place.
- Frame times are the camera's header stamps and pose times the NUC's: the Orin's and the NUC's clocks must agree.
- **The VLM on the Thor.** The executor's VLM conditions post to `vlm_base_url` (default `http://vector-thor:8090/v1`), but the Thor now runs only the Cosmos daemon and `ground_server`. Start `ros2 run orchestrator cosmos_vlm_server --port 8090` on the Thor, in the environment that runs `ground_server`: it answers with the Cosmos daemon, one request at a time, and waits while `ground_server` uses it. `vlm_timeout_s` (executor, default 40 s) covers that wait.

## Run on a laptop, with no robot

```bash
ros2 launch orchestrator orchestrator.launch.py fake:=true planner_backend:=scripted
ros2 topic pub --once /orchestrator/task std_msgs/msg/String "{data: 'Go to the elevator and call it'}"
ros2 topic echo /orchestrator/trace --field data
```

All nodes load `orchestrator/config/orchestrator.yaml` (`params_file:=` for another). A places file is this package's format (`name: {x, y, yaw}`) or the team's map format (`places:` with `yaw_deg`, e.g. `~/maps/two_floor_clean.places.yaml`), whose names become snake-case keys: "freight elevator call panel" is `{freight_elevator_call_panel}`. On the robot, run `trees/freight_call_elevator_agentic.xml` with that file; `docs/RUNBOOK.md` has the bring-up. `contracts_directory`, `places_file` and `vlm_base_url` are launch arguments, passed to every node.

`fake_robot` has the same topics, services, navigation and arm actions, and VLM server as the robot. The two actions need `navigation_interfaces` (from `navigation_stack`) and `humanoid_manipulation_interfaces` built in the workspace; without one, its skill fails with no server. Parameters: `press_success:=false`, `press_fail_count:=1` (the first presses stall 0.13 m short, as out of reach, with feedback), `nav_blocked:=true` (every route turns blocked until cancelled; navigate_to fails after 10 s), `vlm_answers:=door=closed`, `nav_time_s:=5.0`, `vlm_port`.

Run one tree with no planner:

```bash
ros2 run orchestrator send_tree skill_registry/trees/call_elevator.xml --blind
ros2 run skill_registry validate_tree my_tree.xml --blind      # check only
```

## Planner backends

| `planner_backend` | Needs |
|---|---|
| `claude` (default, model `claude-opus-5`) | `pip install anthropic`, and `ANTHROPIC_API_KEY` or an `ant auth login` profile |
| `openai_compatible` | `base_url`, `model`, and the key in the env var that `api_key_env` names. For GPT, Gemini's OpenAI endpoint, vLLM |
| `scripted` | Nothing. It replies with `scripted_tree` |

## Not done yet

- `places.yaml` has zeros. Measure the poses on the prior map.
- Timeouts in the contracts are guesses. All skills are `wip` or `untested`.
- The service names under `/unitree_interface/` and `/slam_elevatorlio/` come from the `~/` names in the code. Check them on the robot.
- `ground`, `set_profile`, `set_hand`, `save_map` and `load_map` need their interface packages on the NUC.
- `reset_tracker` ends only when `/track_3d/is_tracking` changes. If it is already false, it times out.
- `body_ekf` has no reset service, so no tree can reset it at the panel; the team runs `~/ekf_reset.sh` on the NUC by hand right before a press. A `~/reset` service in `body_ekf` and a `reset_ekf` skill would close this (docs/RUNBOOK.md, "foot_world drifts after a walk").
- A failed `press_reached` does not reach `manipulation_recovery`: `get_failure` records only failed actions and failed `button_lit` checks, so the agent sees `failure: none recorded`. Record any failed check that follows a press, with that press as `checked_call`.
- The executor reads the raw camera topic (`image_topic`, default `/camera/camera/color/image_raw`), because the Orin has no compressed image transport installed, and encodes the frames it keeps to JPEG. That brings the raw stream to the NUC: about 2.8 MB a frame, about 17 MB/s at 6 fps.
- On hold: in blind mode, every action skill must have a fallback.
