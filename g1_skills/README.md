# g1_skills

The G1's skills for the orchestrator, one folder each. A folder holds everything about its skill: the contract, the code
it needs and its agent prompts. To add or change a skill, you edit one folder. What the three agent skills decide is in
`docs/agents.md`.

## A skill folder

| File | When | What it holds |
|---|---|---|
| `contract.yaml` | always | What the skill needs, does and how it fails; how the executor reaches it (`interface`) |
| `skill.py` | custom logic | One `Skill` subclass: code run before, instead of, or around the transport's call |
| `spec.py` | agent skills | Phases and their decisions, decision fields, who answers, the limits the executor enforces |
| `agent.py` | agent skills | One `AgentSkill` subclass: the evidence a call gathers, the executor's own rules, decision checks, outputs |
| `prompt.py` | agent skills the model answers | The model's system prompt and the prompt built from each call |
| other modules | when useful | Code only this skill uses, e.g. `press_button/capture.py`, `manipulation_recovery/rules.py` and `stance.py` |
| `__init__.py` | when the folder has code | Empty |

What the team has seen about a skill goes in its contract's `notes`; open problems go in `docs/architecture.md`.

Code that two or more skills share goes in `lib/`: `press.py` (the press offset in force, the last press and its
images for a call), `tracking.py` (what track_anything publishes), `text.py` (number formatting and the panel type for
prompts).

## Four ways to extend

### 1. A new skill on an existing transport: `contract.yaml` only

The built-in transports are `action`, `service`, `topic`, `watch` (a condition on a topic), `vlm` (a closed question
about the camera image), `compare`, `alive`, `command`, `agent` and `python`.

```yaml
name: cancel_nav
kind: action
device: nuc
description: Stop the current route goal
interface:
  transport: topic
  name: /cancel_goal
  ros_type: std_msgs/msg/Bool
  fixed:
    data: true
  success_when:                 # done once the route planner says idle
    topic: /route_planner/status
    ros_type: std_msgs/msg/String
    json: true
    field: state
    equals: idle
    latched: true
resources:
- base
timeout_s: 3
maturity: wip
```

More examples: `navigate_to` (action, with `failure_when` on its feedback), `set_profile` (service with an enum),
`door_open` (watch), `at_floor` (watch compared with an input through `equals_input`), `ask_vlm` (VLM).

### 2. Custom logic around a transport: add `skill.py`

The transport still makes the call. The skill adds to it. Every method is optional:

| Method | Runs | Use it to |
|---|---|---|
| `setup()` | once at startup | subscribe, start timers, make shared state |
| `before(inputs)` | before each call | refuse the call (return a `DoneHandle`), or note its inputs |
| `start(inputs)` | each call | run the whole call yourself (return a handle); `None` leaves it to the transport |
| `wrap(handle, inputs)` | after the call starts | watch the running call: `handle.on_end`, and `handle.on_feedback` for actions |
| `shutdown()` | at exit | stop what `setup` started |

The smallest real example, `set_press_offset/skill.py`, keeps the offset it sends for the press skills:

```python
from orchestrator.skills.api import Skill

from ..lib.press import press_state


class SetPressOffset(Skill):
    def before(self, inputs: dict):
        press_state(self.ctx).offset = tuple(float(inputs.get(axis, 0.0)) for axis in 'xyz')
```

`press_button/skill.py` is the larger one. `before` refuses a press until an offset is set, and `wrap` starts the
three-frame capture (`capture.py`) on the action's feedback.

The skill reads its own settings from `config` in its contract (`self.config`), for example `config.capture` of
`press_button`. Through `self.ctx` it reaches what the executor shares:

| `self.ctx.` | What |
|---|---|
| `node`, `logger`, `now()` | the executor's node, its logger, ROS time in seconds |
| `action_client()`, `service_client()`, `publisher()`, `watch()` | ROS clients, made once and shared |
| `camera` | the latest frame (`jpeg`, `size`, `frame_id`), the intrinsics (`k`) and the last seconds of frames (`frames`) |
| `tf` | `lookup()` (from any thread, with a timeout), `pose_in()`, `numbers()` (a transform as plain numbers) |
| `shared(key, factory)` | one object per key for the whole executor: how skills hand state to each other |
| `submit('vlm' or 'commands', function, ...)` | run blocking work in a thread |
| `artifacts_dir`, `server_wait_s`, `vlm_client` | where to save files for the agent and the recorder; the server wait; the VLM |

### 3. A fully custom skill: `transport: python`

When no transport fits, set `transport: python` and implement `start()`, which returns a handle (`orchestrator.skills.api.Handle`
or one of `orchestrator.skills.transports`). List what must be up for the skill to be live under `interface.requires`
(`services`, `published`, `subscribed`). A condition may use `python`; its code must only read.

```yaml
interface:
  transport: python
  name: /check_reach
  ros_type: humanoid_manipulation_interfaces/srv/CheckReach
  requires:
    services: [/check_reach]
config:
  target:
    pose_topic: /track_3d/selected_normal_pose_filtered/arm_upright
  limits:
    max_age_s: 2.0
```

See `check_reach/skill.py` (a service call with its own request and verdict) and `button_in_view/skill.py` (seed the
tracker, then judge what it sees).

### 4. A decision by a model or the operator: an agent skill

Set `transport: agent` and `interface.skill` to the folder name. Then add:

- `spec.py`: `PHASES` (phase to its decisions), `GOING_ON` (the decisions after which the tree goes on), `FIELDS`
  (decision fields beside decision and reason, with their JSON types), `ANSWERED_BY` (`'model'` or `'operator'`) and
  `limits()`. The contract's `phase` input lists the same phases.
- `agent.py` (optional): an `AgentSkill` subclass. `refuse()` fails a call before it opens; `evidence()` adds to what
  the call carries; `rule()` decides plain cases without the bridge; `task_lines()`, `request_fields()` and `images()`
  say what goes over the link; `check()` refuses decisions outside the limits; `outputs()` is what the tree gets;
  `after_decision()`, `record_fields()` and `finished()` keep what a decision leaves behind; `phase_speech` and
  `decision_speech` are what the robot says. The base class in `orchestrator/orchestrator/agent/api.py` documents each.
- `prompt.py` (when the model answers): `system(limits)` and `content(request, images)`, which gives the prompt text and
  the image names to show in order.

`spec.py` and `prompt.py` import only the standard library, each other and `lib/text.py`: the bridge on the laptop
loads them without ROS. `operator_confirm/` is the smallest example; `manipulation_recovery/` the largest.

## Contract fields

| Field | Meaning |
|---|---|
| `name`, `kind`, `device` | the skill's name (its folder's name); `action`, `condition` or `composite`; where the capability runs |
| `description`, `source`, `notes` | what it does, for the planner; the code it wraps; what the team has seen |
| `interface.transport`, `.name`, `.ros_type` | how the executor reaches it, and the ROS name and type |
| `interface.fixed` | message fields always sent |
| `interface.success_when`, `.failure_when` | a topic value that ends the skill; an action feedback value that fails it once held |
| `interface.question`, `.choices`, `.success_answer` | `vlm` only |
| `interface.skill` | `agent` only: the agent skill's folder |
| `interface.alive`, `.command` | `alive` and `command` only |
| `interface.requires` | `python` only: services and topics that must be up for the skill to be live |
| `inputs`, `outputs` | ports: `string`, `int`, `float`, `bool`, `pose`, `enum`; `field` is the message field, which may index a sequence |
| `config` | the skill's own settings, read by its `skill.py` |
| `resources`, `timeout_s`, `record` | exclusive resources; the call's timeout; a condition whose every answer goes in the history |
| `pre`, `effects`, `fallback`, `needs_cloud`, `maturity` | for the planner and the validator |

## After a change

- Run the tests from this package: `python3 -m pytest -q test` (with `orchestrator` and `skill_registry` on the path).
- Check a tree against the contracts: `ros2 run skill_registry validate_tree <tree.xml>`.
- Rebuild one package at a time, with one worker: `MAKEFLAGS=-j1 nice -n 19 ionice -c3 colcon build --parallel-workers 1 --packages-select g1_skills`.
- A change to an agent skill's `spec.py` or `prompt.py` reaches the model only when the laptop's bridge runs the same
  checkout: pull and restart it.

A new transport (rare) is a `Driver` subclass in `orchestrator/orchestrator/skills/transports.py`, added to
`default_drivers()` there, and its name added to `TRANSPORTS` in `skill_registry/skill_registry/contract.py`.
