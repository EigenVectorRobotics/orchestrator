# Orchestrator

Agentic task planning for the G1. A task is one sentence. An agent does it step by step: it runs one skill, reads the result, and decides the next step. The robot starts with no skills: the agent writes them. Every step goes to a trace topic and to a run bundle.

| Package | Holds |
|---|---|
| `skill_registry` | The skill library (`skills/`, one folder per skill) and what a skill is: the contract schema and grammar, the deny list, the step gate's quiet list, the tree validator, `write_skill`, the ROS type checks, the message conversion, and the skill stamps |
| `orchestrator` | Executor, task desk, step gate, ROS skill runner, code skills and their sandbox, knowledge store, recorder, Mission Control, fake robot. The agent's runbook and codebase docs (`agent/`) |

`<agent_dir>` is `$EIGEN_VECTOR_WS/agent` (launch argument `agent_dir`). `scripts/link_agent_dir.sh` makes it. It holds links to what is in git (`skills/`, `runbook.md`, `docs/`). It also holds what changes on each run (`knowledge/`, `stamps.yaml`). Thus, a skill or a doc that the agent writes is a change in git. A person reviews it and commits it.

## How it works (task mode, the default)

1. A task comes in on `/orchestrator/task` (or `POST /api/task` from Mission Control): a plain sentence, or `{"task": "...", "blind": true}`.
2. The executor opens it: one task is one run, one bundle, one scratch folder, one blackboard. The open message gives the sentence and the places from `map_info`. It also gives the runbook (what the robot is), the contract grammar (how to write a skill) and the knowledge store's guide.
3. The agent plans (a list of subgoals), then runs ONE skill per step. It sees each result and plans again when something fails. It ends with `done` or `give_up`.
4. On the robot, a person approves each step that can move it (the step gate, `step_gate:=auto`: on unless `fake:=true`). `skill_registry/gate.yaml` lists the few acting skills that do not ask (`quiet:`).
5. The trace goes to `/orchestrator/trace` (JSON) and `/orchestrator/log` (`rcl_interfaces/Log`, for Foxglove). The recorder keeps each run in a bundle (`docs/recorder.md`).

Limits: `task_max_steps` 40, `task_max_s` 900, `task_idle_s` 120. Design: `docs/task-mode.md`.

### The task session

The task agent is a Claude Code or Codex session outside the executor, with a shell. It drives the task with `agent_cli` and reads and writes knowledge with the `knowledge` CLI. Start it in `$EIGEN_VECTOR_WS/agent`: there, `.claude/settings.json` (a link to `orchestrator/agent/claude_settings.json`) denies the `ros2` commands that act (`topic pub`, `service call`, `action send_goal`, `param set`, `run`, `launch`, ...), `agent_cli step approve|refuse`, `~/orchestrator_ws`, and edits to the deny list, the gate, the runbook and these settings. Thus the session uses `agent_cli` to act on the robot, and the executor's checks see each step. This is a guard against mistakes. These are not matched: a wrapped command (`bash -c`, a Python script with `rclpy`), and an HTTP request to Mission Control. A Codex session does not read these settings.

One task is one session: start a new session for the next task. Skill discovery is a separate session (`docs/skill-discovery.md`); a task session uses the contracts and does not read the stack's code.

```bash
agent_cli task wait                     # the open task
agent_cli skills                        # what run can run now
agent_cli interface /odom_base_link     # the type and fields of a topic, service or action
agent_cli plan "walk to the call panel" "call the elevator"
agent_cli run say text="Walking to the call panel." --subgoal "walk to the call panel"
agent_cli write-skill greet.yaml --code greet.py
agent_cli done --reason "arrived"
```

`agent_cli run ... --applies <claim id>` says that the step follows a claim of the knowledge store (give it again for each claim). The step's end changes no claim. After the step, the session stages a grade for each claim: `agent_cli grade <id> useful|misleading|wrong|not-applicable --reason R` (the last grade wins; it also works after the task ends). After the task ends, `agent_cli commit` puts the staged grades in the store, like a database commit. If the session does not commit, the next task open does it. Each grade is attributed to the code of its step: a grade of a skill that the agent rewrote later is kept but not counted. `done` and `give-up` name the claims with no grade, but they do not stop the end.

## Skills

The library starts empty. The agent writes each skill with `write_skill` (`docs/skill-discovery.md`), which checks it and loads it at once. One YAML file is one skill. The grammar the agent follows is rendered from the parser (`skill_registry/grammar.py`), so what the agent is told and what `write_skill` takes cannot drift apart. No code is necessary if the skill is an action, service, topic, watch or VLM question.

- An input's `field` can index a sequence: it can set `parameters.0.value.double_value` of an `rcl_interfaces/srv/SetParameters` request whose `fixed` lists the parameters.
- A pose input takes a place (`{inside_freight_elevator_button_panel}`) or a literal `"x y yaw"` or `"x y z yaw"` in `map`; give z on the two-storey map, or the route planner takes the lowest storey. A pose output comes back as `{x, y, z, yaw, frame}`.
- Two read-only transports run inside the executor: `graph` checks that each listed topic has at most its allowed publishers, and `compare` checks that an input equals a value.
- **Skill stamps** (`<agent_dir>/stamps.yaml`, not in git): `write_skill` stamps a skill with the state of its repos and the digest of every skill it runs. When that code moves, the catalog says the skill is stale, with the reason. A step that succeeds is kept as evidence and clears nothing.

### Code skills

A code skill is a skill whose body is Python: a policy over the skills that already exist (Code as Policies). Its contract is `transport: code`, and the Python sits beside it, in `skills/<name>/code.py`:

```python
def run(skills, inputs):
    """Say the greeting, then name every place"""
    skills.run('say', text=inputs['greeting'])

    for place in inputs['places'].split(';'):
        result = skills.run('say', text=f'I know {place}')      # result.ok, result.message, result.outputs

    return {'places': len(inputs['places'].split(';'))}
```

- **It reaches no ROS.** The code runs in a sandbox under an audit hook: no ROS, no network, no new processes, writes only in its scratch folder, and no reads of `~/orchestrator_ws`. This is a guard against mistakes, not a security boundary: the NUC allows no user namespaces. `skills.run` sends each call back to the executor, which runs it like any step. So the deny list, the contract, the places and the recorder all still see it.
- **The gate.** Every call it makes that can act on the robot asks again; a call that only reads never does.
- **Writing one.** `write_skill` takes the contract and the code together and checks both (the code must parse and define `run`); if either is refused, neither is kept.
- **Building on a policy.** A code skill may call another, but not one that is already running in that chain. Three code skills deep is the limit (`MAX_DEPTH`), because each one is a process.
- **Limits.** The contract's `timeout_s` ends the whole policy, the inner calls with it.

## Knowledge store

What the robot learns is kept as claims in `<agent_dir>/knowledge/` (`docs/knowledge.md`). A claim is an observation, a belief or a fact.

- **A claim about a skill is about its code.** It keeps a snapshot of the code under the skill and the skills it runs. When that code moves, its old sightings stop counting and it goes back to an observation.
- **A contradiction moves a claim down one tier.** A grade of `useful` or `wrong` is weak evidence: it counts in the claim's reliability but moves no tier. `misleading` and `not-applicable` are kept apart: `knowledge list` says to make the claim's text or tags narrower.
- **Only the operator makes a fact,** at a terminal: `approve`, `refuse`, `retire`, and `--by operator`.

```bash
ros2 run orchestrator knowledge list --ready                  # beliefs that can become facts
ros2 run orchestrator knowledge show C-0007
ros2 run orchestrator knowledge note "The call panel does not light." --by agent:task --evidence step:3 --run <run_id> --about place=freight_elevator_call_panel --key lights_when_pressed --value false
ros2 run orchestrator knowledge approve C-0007 [--text "the exact text"]
```

## Mission Control

`mission_control:=true` serves a page on `127.0.0.1:8080` (`mission_control_port`): the task, the trace, the camera, and the step gate's approve and refuse. It listens on localhost only; reach it over `ssh -L 8080:localhost:8080`. The node log gives the link with its token. A command without the token is refused, and each refusal is logged.

## Run on a laptop, with no robot

```bash
ros2 launch orchestrator orchestrator.launch.py fake:=true
ros2 topic pub --once /orchestrator/task std_msgs/msg/String "{data: 'Tell me where you are'}"
ros2 topic echo /orchestrator/trace --field data
```

The library starts empty, so the agent must write its skills first. To exercise the engine alone, run the tests: they use a small made-up robot (`skill_registry/test/skills`).

All nodes load `orchestrator/config/orchestrator.yaml` (`params_file:=` for another). `fake_robot` has the same topics, services, navigation and arm actions, and VLM server as the robot. The two actions need `navigation_interfaces` (from `navigation_stack`) and `humanoid_manipulation_interfaces` built in the workspace; without one, its skill fails with no server. Parameters: `press_success:=false`, `press_fail_count:=1`, `nav_blocked:=true`, `vlm_answers:=door=closed`, `nav_time_s:=5.0`, `vlm_port`.

## Run on the robot

The NUC builds this repo in `~/eigen_vector_orch_ws`, from the deploy repo `eigen_vector_orch` (`repos/nuc.repos`). In that repo:

```bash
./scripts/deploy.sh update nuc        # pull and build (about 2 min); it starts and stops nothing
./scripts/deploy.sh launch nuc map:=/home/vector/maps/two_floor_clean_cabin2.pcd localization:=true orchestrator:=true mission_control:=true
```

- **The NUC is shared.** Before a launch, check `tmux ls` and `ps -eo pid,etime,args | grep -E '[r]os2 launch|[l]ocal_planner'`. When someone else is up, use another `ROS_DOMAIN_ID` (for example 71).
- **Mission Control:** `ssh -L 8080:localhost:8080 vector@vector-nuc`, then open the link with its token from the node log (`mission control on ...`).
- **The step gate is on.** Approve or refuse each step in Mission Control, or at a terminal with `agent_cli step approve|refuse`.
- **VLM skills** (for example `ask_vlm`) post to `vlm_base_url`, default `http://vector-thor:8090/v1`. On the Thor, start `ros2 run orchestrator cosmos_vlm_server --port 8090` in the environment that runs `ground_server`. It answers with the Cosmos daemon, one request at a time.
- **Once after a deploy:** `scripts/link_agent_dir.sh` (safe to run again). It moves a library in the old layout (`agent/contracts/`, `agent/code/`) into `skill_registry/skills/`.
- **First checks of a launch:** the log says `runbook: ...` and no `broken link`; it says `skill stamps: N repos under ...`, and `$EIGEN_VECTOR_WS/agent/knowledge/code_clock.yaml` exists.

## Not done yet

- Nothing committed after 2026-11-11 has run on the robot.
- A Codex session has no deny rules yet (Claude Code settings only).
- The knowledge store follows a claim's `skill` tag only: a value measured through a skill but tagged only with a place is not reset when the skill's code moves. A code skill's `skills.run(variable)` calls are not followed.
- The executor reads the raw camera topic (`image_topic`), because the Orin has no compressed image transport installed: about 17 MB/s at 6 fps.
