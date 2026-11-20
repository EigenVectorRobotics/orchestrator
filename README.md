# Orchestrator

Agentic task execution for a Unitree G1 that rides a freight elevator and presses its buttons. An LLM writes a behavior tree, a validator checks it, and the robot's NUC runs it. An agent on the operator laptop judges each press and decides how to recover from a failed one. Every step goes to a trace topic and to a recorded run folder.

## How it works

1. A task comes in on `/orchestrator/task`, as a sentence.
2. The planner first runs the preflight tree: stacks up, localization healthy, the G1 in ready locomotion.
3. The planner gives the task, the skill contracts, the places, the live skills and the tree library to an LLM, which answers with a BehaviorTree.CPP v4 tree or picks a library tree.
4. The validator checks the tree against the contracts; errors go back to the LLM, `max_attempts` times at most.
5. The executor ticks the tree at `tick_hz` and calls each skill through its contract's transport (a ROS action, service or topic, a topic watch, a VLM question, an agent call), with the skill's own code around the call when it has some.
6. Agent calls (`press_judge`, `manipulation_recovery`, `operator_confirm`) go to the agent bridge on the laptop, which asks a Claude model or the operator for one decision; the executor checks it against fixed limits.
7. The run recorder keeps everything on the NUC under `~/orch_runs`; the Run Viewer shows it in a browser.

| Package | Holds |
|---|---|
| `g1_skills` | The G1's skills, one folder each: the contract, and the skill's own code, agent spec and prompts when it has them (`g1_skills/README.md`) |
| `orchestrator` | The core, which names no skill: tree executor, skill runner and transports, agent engine and bridge link, planner, trace, recorder, health checks, VLM server, fake robot; `test_trees/`, trees for tests and runs (not shown to the planner) |
| `skill_registry` | The contract model, the tree validator, live-skill discovery, the tree library (`trees/`) and the preflight tree (`preflight/`) |
| `mission_control` | The agent bridge (laptop), and the robot run scripts (`robot/`) |
| `run_viewer` | The Run Viewer (not a ROS package): the tree, map, camera and agent calls of a run in a browser |

## Documents

The site at https://eigenvectorrobotics.github.io/orchestrator/ shows these documents with their diagrams and an API reference generated from the code: what each package and module holds is in its docstrings there. A push to `main` builds the site (`.github/workflows/docs.yml`) into `github-pages/main`, which Pages serves. The team's other repositories are linked from https://eigenvectorrobotics.github.io/eigen_vector/.

| Document | Read it when |
|---|---|
| `CLAUDE.md` | You are an agent working in this repository: start here |
| `docs/design/system.md` | You want the diagrams: machines and packages here, then `docs/design/classes.md`, `docs/design/sequences.md` and `docs/design/trees.md` |
| `docs/architecture.md` | You change the planner, validator, executor, contracts, preflight or trace, or need the open problems |
| `docs/agents.md` | You work on an agent skill, the bridge's prompts, the executor's rules or the limits |
| `g1_skills/README.md` | You add or change a skill |
| `docs/run.md` | You run the robot: deploy, start, run a task or one tree, bring a machine up, stop, watch |
| `docs/demo.md` | You are on site for the elevator demo and need the checklist |
| `docs/machines.md` | You need what runs where on the NUC, Orin and Thor, or what a robot computer must change |
| `docs/troubleshooting.md` | Something goes wrong on the robot: symptom, checks, fix |
| `docs/recorder.md` | You read a recorded run or change what the recorder keeps |
| `mission_control/README.md` | You set up or run the agent bridge, or answer `operator_confirm` |
| `mission_control/robot/README.md` | You start, run, watch, cancel or stop a robot run from the laptop |
| `run_viewer/README.md` | You watch a run in the browser or replay a recorded one |

Each library tree in `skill_registry/trees/*.xml` says in its first comment what it does and its parameters.

## Run on a laptop, with no robot

```bash
ros2 launch orchestrator orchestrator.launch.py fake:=true planner_backend:=scripted places_file:=<a places file on this laptop>
ros2 topic pub --once /orchestrator/task std_msgs/msg/String "{data: 'Go to the elevator and call it'}"
ros2 topic echo /orchestrator/trace --field data
```

`fake_robot` stands in for the robot: the robot's topics, services, navigation and arm actions, and a VLM server on `vlm_port`. The two actions need `navigation_interfaces` and `humanoid_manipulation_interfaces` built in the workspace; without one, its skill fails with no server. Its parameters (`orchestrator/orchestrator/fake_robot.py`): `press_success:=false`; `press_fail_count:=1` (the first presses stall short, as out of reach, with feedback); `nav_blocked:=true` (every route turns blocked until cancelled, so `navigate_to` fails after its `failure_when` `hold_s`); `vlm_answers:=door=closed`; `nav_time_s:=5.0`. Without a site no agent bridge is set, so agent skills fail. A fake run has no preflight (it would try to start the robot stacks and fail on the missing NUC topics); `preflight:=true` runs it anyway. The planner backends are in `docs/architecture.md`.

Run one tree with no planner:

```bash
ros2 run orchestrator send_tree my_tree.xml --blind            # validate and run it with no cloud link
ros2 run orchestrator send_tree my_tree.xml --skip-preflight   # before any preflight passed: run it anyway (tests)
ros2 run skill_registry validate_tree my_tree.xml --blind      # check only
```

## Add a skill

A skill is one folder in `g1_skills/g1_skills/`. A skill on a ROS action, service or topic, a topic watch, a VLM question or one of the executor's own transports needs only its `contract.yaml`. How to write one, and the four ways to extend a skill, are in `g1_skills/README.md`; what the executor does with each transport is in `docs/architecture.md`, "Skills and contracts".
