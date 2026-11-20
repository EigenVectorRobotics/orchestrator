# Architecture

How the orchestrator turns a task into robot motion: the nodes, the skills and their contracts, the checks before and during a run, and what is still open. How to run it on the robot is in `docs/run.md`; how the agent calls work is in `docs/agents.md`.

## The parts

| Node or program | Where | Job |
|---|---|---|
| `registry_node` (`skill_registry`) | NUC | Loads the contracts and publishes `/skills/live` |
| `planner_node` | NUC | Runs the preflight, asks the LLM for a tree, validates it, sends it to the executor |
| `executor_node` | NUC | Validates a tree again and ticks it; calls each skill from its contract; opens agent calls |
| `run_recorder` (node `orchestrator_recorder`) | NUC | Writes every run to `~/orch_runs` (`docs/recorder.md`) |
| `fake_robot` | laptop | The robot's topics, services, actions and a VLM server, for tests (`fake:=true`) |
| `mission_control/agent_bridge.py` | operator laptop | Answers every agent call (`docs/agents.md`, `mission_control/README.md`) |

`orchestrator/launch/orchestrator.launch.py` starts the registry, the executor, the planner and, unless `record:=false`, the recorder. All nodes load `orchestrator/config/orchestrator.yaml` (`params_file:=` for another). Every node loads the contracts from the skill packages' folders (parameter `skill_packages`, default `DEFAULT_SKILL_PACKAGES` in `skill_registry/skill_registry/registry.py`), or from one folder when the launch argument `contracts_directory` names it; `contracts_directory`, `places_file` and `vlm_base_url` are launch arguments passed to every node. A site file (`site:=stanley`, `orchestrator/config/sites/stanley.yaml`) gives the places file ([The map](run.md#the-map)), the planner backend, the agent bridge's address and the raw bag; a launch argument given on the command line wins over the site. Without a site no agent bridge is set, so agent skills fail.

## A task, step by step

1. A task comes in on `/orchestrator/task`: a plain sentence, or `{"task": "...", "blind": true}`.
2. The planner sends the preflight tree (`skill_registry/preflight/preflight.xml`) to the executor and waits for it to succeed (`preflight_timeout_s`). If it fails, the planner does not plan.
3. The planner sends the task, the skill catalog, the places, the live skills and the tree library (`skill_registry/trees/*.xml`) to the LLM. The LLM answers with a BehaviorTree.CPP v4 XML tree, or picks a library tree with `<use_tree id="..." name="value">`, optionally with `<replace subtree="..." with="..."/>`.
4. The validator checks the tree. If there are errors, the planner sends them back to the LLM, `max_attempts` times at most.
5. The planner publishes the tree on `/orchestrator/tree` (or `/orchestrator/tree_blind`). The executor checks that the recorder is ready, validates the tree again, checks the preflight gate, then ticks it at `tick_hz`.
6. Each leaf is a skill. The executor calls it as its contract says. An agent skill opens a call to the agent bridge (`docs/agents.md`).
7. Every step goes to the trace; the recorder writes it all to the run's folder.

## The code

The core, `orchestrator/orchestrator/`, runs trees, calls skills through their transports, talks to the agent bridge, plans and records; it names no skill. The diagrams of how its parts fit are in [System](design/system.md), [Classes and interfaces](design/classes.md), [Sequences](design/sequences.md) and [Trees](design/trees.md). The skills live in `g1_skills/g1_skills/`, one folder each. What each package and module holds is in its own docstrings (each package's `__init__.py`), which the API reference on the docs site shows.

## Topics

| Topic | What |
|---|---|
| `/orchestrator/task` | A task for the planner |
| `/orchestrator/plan` | The planner's last tree (latched) |
| `/orchestrator/tree`, `/orchestrator/tree_blind` | A tree for the executor; JSON `{"tree": XML, "skip_preflight": true}` from `send_tree --skip-preflight` |
| `/orchestrator/cancel` | `std_msgs/Empty`: halt the running tree |
| `/orchestrator/status` | The executor's state (latched) |
| `/orchestrator/trace` | Every planner and executor event as JSON |
| `/orchestrator/log` | The same events as `rcl_interfaces/Log`, for the Foxglove Log panel |
| `/orchestrator/recorder/status` | The recorder's `ready`, `reason`, `free_gb`, `session` (latched) |
| `/orchestrator/agent/request`, `/reply`, `/event` | Each agent call, the executor's replies, the bridge's reports |
| `/orchestrator/planner/request`, `/answer` | The `external` planner backend's open call and its answer |
| `/skills/live` | The live skills (latched JSON list) |
| `/speak` | Sentences for the robot's text to speech |

All names are in `orchestrator/orchestrator/common/topics.py`.

## Skills and contracts

A skill is one folder in `g1_skills/g1_skills/`: its `contract.yaml` (schema: `skill_registry/skill_registry/contract.py`) and, when it needs them, its own code. The folder's files, the contract's fields and the four ways to extend a skill are in `g1_skills/README.md`; the orchestrator core names no skill. The executor needs no code for a new skill on one of these transports (`orchestrator/orchestrator/skills/transports.py`):

| Transport | Example | What the executor does |
|---|---|---|
| `action` | `navigate_to`, `press_button` | Sends a goal; `failure_when` fails it early on a feedback value held for `hold_s` |
| `service` | `set_profile` (enum input), `set_press_offset` | Calls the service; a reply with per-item `successful` flags fails if any is false; with `success_when`, waits until a topic shows the result |
| `topic` | `cancel_nav` | Publishes one message once a subscriber is there; with `success_when`, waits until a topic shows the result |
| `watch` | `door_open`, `at_floor` | Reads a topic and compares a field; `equals_input` compares with a tree input |
| `vlm` | `ask_vlm` | Asks the VLM a closed question about the latest camera frame |
| `python` | `button_in_view`, `check_reach` | Nothing of its own: the skill's `skill.py` runs every call. `interface.requires` names what must be up for it to be live |
| `agent` | `press_judge`, `manipulation_recovery`, `operator_confirm` | Opens an agent call (`docs/agents.md`) |
| `compare`, `alive`, `command` | `decision_is`, `nuc_up`, `launch_nuc_stack` | Runs inside the executor: compare two values, check that topics, services or URLs exist, run a shell command |

- A pose input takes a place (`{inside_freight_elevator_button_panel}`) or a literal `"x y yaw"` or `"x y z yaw"` in `map`. Give z on the two-storey map, or the route planner takes the lowest storey.
- Every finished action goes in the history; a condition does only with `record: true` (`check_reach`), and then every answer does, a pass included.
- A places file is this package's format (`name: {x, y, yaw}`) or the team's map format (`places:` with `yaw_deg`, and a `floors` table). Names become snake-case keys: "freight elevator call panel" is `{freight_elevator_call_panel}` (`orchestrator/orchestrator/common/config.py`).

## Live skills

`registry_node` loads every contract from the skill packages (or `contracts_directory`) and checks the ROS graph once a second. A skill is live when what its contract calls is in the graph now: an action server, a service server, a topic's subscriber (and the publisher of its `success_when` topic), a watched topic's publisher, what a `python` skill's `interface.requires` names; a `vlm` skill while `vlm_base_url/models` answers (probed every 5 s). `agent`, `compare`, `alive` and `command` skills are always live. The registry publishes the sorted list on `/skills/live` (latched) when it changes.

The planner subscribes to `/skills/live` (`use_live_skills`), tells the LLM to use only those skills, and its validator refuses a skill that is not live. Until the first list arrives, there is no such check. The executor does not check liveness: it loads every contract at start.

## The validator

`skill_registry/skill_registry/validator.py`, used by the planner, the executor, `send_tree` and `ros2 run skill_registry validate_tree <tree.xml> [--blind]`. It refuses a tree when:

- the XML is not a BehaviorTree.CPP v4 tree, a node is unknown, a decorator lacks its attribute or has the wrong number of children, or a SubTree is unknown or calls itself;
- a skill input is missing or unknown, an enum value is not one of its choices, or a `{key}` is neither a place nor written by the tree;
- in blind mode, a skill has `needs_cloud` (the three agent skills);
- a skill on a device outside `essential_devices` (`orchestrator/config/orchestrator.yaml`) is not inside a Fallback with a recovery after it;
- (planner only) a skill is not live;
- a skill is `disabled`, or two Parallel branches use the same resource.

A skill that is not `tested` gives a warning.

## The preflight gate

The preflight tree starts the Run Viewer, starts the NUC, Orin and Thor stacks that are not up (`nuc_up`, `orin_up`, `thor_up`), stops on a control node that runs twice (`no_duplicate_nodes`) and warns of stray processes (`no_stray_processes`), waits for a healthy localization (asking a person to relocalize, two minutes at most), checks that the G1 is in ready locomotion (`robot_ready`), and relocalizes where the robot stands. Its diagram is in [Trees](design/trees.md#preflight-tree).

The executor runs a task tree only after a preflight has passed since the launch (`require_preflight`, set by the launch argument `preflight`, default true, false with `fake:=true`). With `preflight` false the launch also gives the planner no preflight tree (`preflight_tree` empty), so no task runs one. A preflight that does not succeed closes the gate again. `send_tree --skip-preflight` runs a tree anyway and leaves a `preflight_skipped` warning in the trace. The executor also refuses a tree while the recorder is not ready (`require_recorder`, set by `record`).

## Walking into and out of the cabin

The subtrees `walk_in` and `walk_out` (`skill_registry/trees/subtrees/walk.xml`):

- Latched points in `/terrain_map` close ahead can block the walk into the cabin, so a failed walk in, or a failed walk on after the relocalization out of the cabin, says so, clears the terrain map, waits 2 s and walks again, three tries in all.
- Tracking alone rejects the larger correction a ride leaves (0.5 m once), so `walk_out` stops at the goal floor's door-out place, about 1 m out of the door, and relocalizes in place (`relocalize_here`) before walking on.

## Press frames

`press_button`'s `skill.py` keeps three camera frames around each press for `press_judge` (baseline, press, release), each also cropped around track_anything's box. Through the press it also samples the camera's pose and the gripper's depth fit with the end effector: the first gives the recovery its aim crops, the second moves the residual to the real fingertip, by the fit nearest the deepest point or else the held median of the recent fits, the rule `move_to_pose` corrects its target by. `contact_aim` marks that estimated fingertip with a cyan crosshair, and the agent calls are told which part of the gripper is the fingertip, with operator-labelled references (`g1_skills/g1_skills/lib/fingertip.py`). Not yet tested on the robot. How the frames are chosen and cropped is in `g1_skills/g1_skills/press_button/capture.py`, its settings are `config.capture` in its contract, and what the agent calls get from them, with the camera's frame rate and the clocks that must agree, is in `docs/agents.md`. The frames go to `capture_dir`; the recorder moves them into the run.

## VLM conditions

The executor's `vlm` skills post to `vlm_base_url` (default in `orchestrator/launch/orchestrator.launch.py`) and wait up to `vlm_timeout_s`. On the Thor, `ros2 run orchestrator vlm_server --port 8090` answers with a VLM, the Edge-LLM Cosmos daemon by default (`--model`), one request at a time; a question may carry several images, and the model decides what to do with them (Edge-LLM answers on the first). No tree asks the VLM now (`ask_vlm` is the only enabled `vlm` skill).

## Trace

Every planner and executor step goes to `/orchestrator/trace` as JSON (`time`, `source`, `kind`, `text`, and the event's fields) and to `/orchestrator/log` for Foxglove (`orchestrator/orchestrator/execution/node_setup.py`). The executor's events carry `run_id` and `tree_id`. A finished skill call is a `record` event with its full record (an action's feedback over time included). The event kinds are listed in `docs/recorder.md`. The executor also writes the agent bridge's link state to its ROS log, where the robot scripts read it.

## Recording

`run_recorder` writes one folder per bring-up and one per tree run, with the trace, the records, each agent call, the press frames and a bag. See `docs/recorder.md`.

## Blind mode

A task with `"blind": true`, a tree on `/orchestrator/tree_blind` or `send_tree --blind`: the validator refuses every skill that needs the cloud. Plan such a tree before the robot goes into the elevator. Since the three agent skills need the cloud, a blind tree has no press judge and no recovery.

## Planner backends

| `planner_backend` | Needs |
|---|---|
| `claude` (the launch default; model: launch argument `planner_model`) | `pip install anthropic`, and `ANTHROPIC_API_KEY` or an `ant auth login` profile |
| `openai_compatible` | `base_url`, `model`, and the key in the env var that `api_key_env` names. For GPT, Gemini's OpenAI endpoint, vLLM |
| `scripted` | Nothing. It replies with `scripted_tree` |
| `external` (the stanley site) | A Claude Code session that answers with `planner_cli` (`wait`, `show`, `answer --use-tree ID` or `--tree FILE`), or `planner_cli run --use-tree ID`, which sends the task and answers its call; the planner waits up to `external_timeout_s` per call |

## Open problems

- **Arrivals are poor.** With `path_follower` `slow_down_distance_threshold` 0.7, call-panel arrivals overshot 3 to 6 cm. At the default 0.4 they ranged from 1 cm short to 17 cm past at the call panel and up to 24 cm past inside, up to 10 cm sideways and 9 deg in yaw (agent trial run_235411, run_235844, run_000131, run_001046). The camera looks down, so a closer robot sees the button lower; too close, `button_in_view` fails (no 3D pose, or the box at the bottom edge). Not fixed. It is handled by the look's 2.5 s settle, `button_in_view`, `check_reach`, `rule_stance` and the prompt's S2. Next step: set `slow_down_distance_threshold` 0.7 again (a relaunch resets it to 0.4) and compare, or move the places back by the mean overshoot.
- **When the gripper hides the button outside, the press has likely landed high.** The operator reported it on 2026-11-08, and the logs confirm the missing gripper correction outside: the fingertip hit about 5 mm above centre, often on the upper rim, with the gripper hiding the button. `move_to_pose` ignored a stale gripper estimate outside (the gripper below the frame); inside it applied about 16 mm. The residual follows the gripper's depth fit only when one lies within `tip_shift_max_age_s` of the deepest point, so with a stale fit it still does not show the high landing. The recovery's O1 measures the fingertip against the button centre in `contact_aim`, but a hidden button may leave nothing to measure, and the judge's answer has no "hidden" field. An outside press does not always land high, but a hidden button points to it; then consider a little −z in the press offset. Not yet tested on the robot.
- **The real fingertip ends above the estimated one.** On 2026-11-19 the operator marked the fingertip in the press frames of 7 presses: it sat 23 to 35 mm above, and up to 12 mm right of, where the encoders put `right_ee` (sessions `20261120T003049Z-fa6cae` run 002 and `20261120T004319Z-eed273` run 001; calls `ad93a39a`, `0a12065a`, `06ed7fd3`, `08431ffc`, `422fc3af`). `move_to_pose`'s gripper correction (held median) moved the target 13 to 18 mm down, so after it the fingertip still lands about 15 mm above the goal. Changing the D435 mount between 0.0811 and the hand-tuned 0.0690 did not change this. Suspected: the `right_ee` offset on the wrist, the camera's pitch, or the gripper fit, whose inliers were 0.41 to 0.64. Status: confirmed on the robot, cause not found.
- **The floor 2 call panel place needs fine-tuning.** From `fvd_map_callpanel_tilt.places.yaml` (the site's places file since `3c04eac`) the up button sat 0.47 to 0.56 m ahead, at the edge of the right arm's reach: in sessions `20261119T234750Z-fd820c` and `20261119T235720Z-43f906` most first looks failed `check_reach` and needed one or two repositions, and once three looks in a row failed and the tree stopped. Presses that reached stood with the button 0.41 to 0.47 m ahead. Re-record the place 5 to 10 cm closer to the panel, then check the up and down buttons from it. Status: confirmed on the robot.
- Contract timeouts are guesses. Every contract is `wip` or `untested` (and `door_open_vlm` is `disabled`).
- `ground`, `set_hand`, `save_map` and `load_map` are in no tree and have not run from the executor. The names and values of `ground` and `set_hand` match their providers' code; those of `save_map` and `load_map` come from the `~/` names in the code and are unchecked, as is `vlm_interfaces` on the NUC for `ground`.
- The executor reads the raw camera topic (`image_topic`, default `/camera/camera/color/image_raw`), because the Orin has no compressed image transport, and encodes the frames it keeps to JPEG. That brings the raw stream to the NUC: about 2.8 MB a frame, about 17 MB/s at 6 fps.
- `thor_up` checks the grounding service only; the VLM endpoint is not checked, since no tree asks the VLM.
- `planner_cli answer --use-tree` sets a library tree's parameters (`--param`) but cannot swap its subtrees, though the planner accepts `<replace>` from a model; that needs `--tree FILE`.
- On hold: in blind mode, every action skill must have a fallback. The validator now asks a fallback only of skills on devices outside `essential_devices`.
