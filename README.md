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

Write one YAML file in `skill_registry/contracts/`. No code is necessary if the skill is an action, service, topic or VLM question. See `navigate_to.yaml` (topic), `press_button.yaml` (action), `set_profile.yaml` (service with an enum), and `door_open.yaml` (VLM).

## Run on a laptop, with no robot

```bash
ros2 launch orchestrator orchestrator.launch.py fake:=true planner_backend:=scripted
ros2 topic pub --once /orchestrator/task std_msgs/msg/String "{data: 'Go to the elevator and call it'}"
ros2 topic echo /orchestrator/trace --field data
```

`fake_robot` has the same topics, services, arm action and VLM server as the robot. Parameters: `press_success:=false`, `vlm_answers:=door=closed`, `nav_time_s:=5.0`.

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
- The executor's VLM client needs a JPEG `CompressedImage` topic (`image_topic` parameter).
- On hold: in blind mode, every action skill must have a fallback.
