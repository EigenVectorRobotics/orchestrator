# Orchestrator: rules for Claude Code sessions

This repository runs the Unitree G1's elevator task: a planner picks or writes a behavior tree, the executor on the robot's NUC runs it, and an agent bridge on the operator's laptop answers the tree's agent calls with a Claude model. These rules hold for every session in this repository, for the whole team.

## Where things are, and when to read them

Each document below is read by a session at the moment named; a document that no session needs is not kept.

| Read | When |
|---|---|
| `docs/run.md` | Before you start, run, watch or stop anything on the robot |
| `docs/demo.md` | When the operator wants the short on-site checklist |
| `docs/troubleshooting.md` | First, when something on the robot fails: a machine, localization, a walk, a press, the orchestrator |
| `docs/architecture.md` | Before you change planner, executor, skill or tree code; its **Open problems** section before you analyse a run or change press or walk behaviour |
| `docs/agents.md` | Before you analyse an agent call or change `press_judge`, `manipulation_recovery`, `operator_confirm`, the executor's rules or the bridge |
| `docs/machines.md` | Before you deploy, or touch the NUC, the Orin or the Thor |
| `docs/recorder.md` | When you read a run's records, or change the recorder |
| `mission_control/README.md` | When you set up or run the agent bridge on the laptop |
| `mission_control/robot/README.md` | When you use the robot scripts |
| `run_viewer/README.md` | When you use or change the Run Viewer |
| `README.md` | For the overview and to run on a laptop with no robot |

The single source of each kind of fact:

- **What the model is told**: `mission_control/agent_prompts.py`. No document is given to a model.
- **Phases, decisions, limits, framing of agent calls**: `orchestrator/orchestrator/agent_spec.py`.
- **Skills**: `skill_registry/contracts/*.yaml`. **Trees**: `skill_registry/trees/` (the planner's library) and `orchestrator/test_trees/`.
- **Robot run values** (places file, planner backend, bridge address, raw bag): `orchestrator/config/sites/stanley.yaml`.
- Documents name the constant, parameter or file a number comes from; they do not keep their own copy of it.

## Running the robot

Your part in a run: start the orchestrator, send the task, watch, and cancel when the run goes wrong. Use the scripts in `mission_control/robot/` (`start.sh`, `run_task.sh`, `status.sh`, `cancel.sh`, `stop.sh`); `docs/run.md` has the manual commands.

- **You do not answer agent calls.** The bridge's model answers `press_judge` and `manipulation_recovery`; the operator answers `operator_confirm` in the bridge's terminal. The operator starts the bridge in their own terminal (`mission_control/README.md`).
- **No questions in the middle of a run.** The operator's request to run the task is the go for every step inside it. Report what happened, with the evidence, when the run ends.
- **Ask the operator only for what needs a person**: relocalizing the robot (`/initialpose` from Foxglove), standing it up or removing the gantry, pressing a button the robot asks for, restarting a robot stack. Starting or stopping a robot stack, or anything outside the run, waits for the operator's go unless they already asked for it.
- **Never**: build on the NUC while the robot stands; call `ready_locomotion` on a standing robot; restart the orchestrator while an agent call is open; change anything on the robot computers beyond the run you were asked for.
- When something fails, read `docs/troubleshooting.md`, then the run's records (`docs/recorder.md`) and the Run Viewer.

## Changing code and documents

- Run the tests before every commit: `source /opt/ros/jazzy/setup.bash; export PYTHONPATH=$PWD/orchestrator:$PWD/skill_registry:$PYTHONPATH; PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest -q orchestrator/test skill_registry/test mission_control/test`. Check a changed tree with `ros2 run skill_registry validate_tree <tree.xml>` (0 errors).
- **Delete or rename something, and clean every reference to it in the same change**: documents, code comments, contracts, trees, config. `grep -rn "<name>" --exclude-dir=.git .` finds them.
- **A change in behaviour updates its document in the same change**: a skill or tree in `docs/architecture.md`, an agent call or prompt in `docs/agents.md`, a run step in `docs/run.md`.
- **A new document is added to the table above**, with when to read it. `orchestrator/test/test_docs.py` fails on a document that is not listed, a link that does not resolve, or a file path in backticks that does not exist.
- **Observations from a run** go in `docs/architecture.md` under **Open problems** while nothing handles them: what was seen, the numbers, the evidence (session, run, call id), and its status (confirmed, reported, suspected). A session drafts the text; a person approves it word for word before it goes in. When the code or a prompt comes to handle it, the entry is deleted, and its reason moves, as one sentence, next to the mechanism that handles it.
- A change to a prompt, an executor rule or a tree changes what the robot does: say in the commit that it is not yet tested on the robot until it is.
