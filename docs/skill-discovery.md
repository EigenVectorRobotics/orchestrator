# Skill discovery: the agent seeds its own skill library (design, not built)

Status: draft for review, 2026-11-05. Branch `feat/skill-discovery`. Related: `docs/task-mode.md`, `docs/skill-packages.md` (versions, graduation, compositions), `docs/knowledge.md`.

## Goal

- The agent finds its base skills itself: it reads the stack's code and the live ROS graph, and writes a contract for each interface that it can use. This seeds the skill library.
- The skills then get better with use: graduation counts, new versions after failures, compositions (BTs) from subgoals that work again and again.
- The test: compare what the agent finds and builds with the skills that people wrote (the 32 contracts, the trees, `manipulation_recovery.md`, on `main` and `ltx-dev`).

## What the agent can use

**Everything that the stack exposes, except the deny list below.** That includes, for example:
- every ROS topic, service and action of the stack on the NUC, the Orin and Thor: navigation (`route_planner`, `local_planner`, the SLAM node), manipulation (`move_to_pose`, visual servoing), `map_info` (places, regions, floors, `NavigateToPlace`), track anything on the Orin, the camera stream on the Orin, the VLM interfaces on Thor (the HTTP server and `ground_server`), and the parts of `unitree_interface` that are not denied;
- the stack's code in the workspace's `src/` (read-only), to find out what the interfaces do;
- the orchestrator's own tools: `plan`, `run_skill`, `note` (the knowledge store), `say` (`/speak`), `read_file`, `ros_read`, `get_image`, `run_python`, `done`, `give_up`, and new: `write_skill`, `interface_show` (the type of a topic, service or action);
- the transports that the executor knows (action, service, topic, watch, graph, compare, vlm, press_tip, agent), and `ask_vlm`, the orchestrator's one closed question to the VLM.

People write only a short agent runbook (`docs/agent/runbook.md`): what the robot is, the devices, how to see that the stack is up, the safety rules and the deny list, the places file. It describes the robot and the rules, not the skills or the steps of a task. The agent writes the codebase docs itself during discovery (`<workspace>/agent/docs/<package>.md`).

## What the agent cannot use

**Denied, enforced in code** (built: `skill_registry/skill_registry/deny.yaml`, the operator's file; `DenyList.check` runs when a contract is loaded or announced, and will run in `write_skill`; a denied contract is left out with a warning; the agent sandbox blocks the listed modules):
- low-level control: every `rt/*` DDS topic (`rt/lowcmd`, `rt/lowstate`, `rt/arm_sdk`, `rt/dex3/*`, ...), any `lowcmd` topic, the Unitree SDK (`unitree_sdk2`, `unitree_sdk2py`);
- `/unitree_interface/change_mode` to `LOW_LEVEL` (4), `EMERGENCY` (3) or `MONOSTATE` (0): a contract for it may map only `idle` (1) and `high_level` (2);
- `/unitree_interface/ready_locomotion` (it damps the robot and stands it up again).

**The session too.** In this experiment the agent is a Claude Code session with a shell on the NUC. The checks above cover what goes through the executor and the sandbox; a shell can still call `ros2 service call` directly. So the session runs with its own permissions: shell commands only through `agent_cli` (without `step approve|refuse`) and read-only tools, set in the session's config (step 3).

**Not on this branch** (2026-11-09, branch `agentic`): the hand-written skills, that is all 32 contracts (`ask_vlm` too), the trees, the agent skills `manipulation_recovery.md` and `press_judge.md`, and the experience log. They stay on `main` and `ltx-dev` for the comparison. The agent starts with zero skills; it can write `ask_vlm` itself, because the `vlm` transport is part of the executor and the `vlm` package is in `src/`. If it does not find it, we seed it.

**Hidden for the test** (out of the agent's view):
- the orchestrator's source and `skill_registry/`: the tests keep copies of some contracts and trees in `skill_registry/test/data/`;
- `docs/RUNBOOK.md` (Tingxuan's), because it gives the steps of the elevator task.
- How: the launch argument `contracts_directory` defaults to the agent's folder (`<agent_dir>/contracts`, e.g. `~/eigen_vector_orch_ws/agent/contracts`, empty at the start), and `agent_read_roots` leave out the hidden paths.

## Discovery

1. **A discovery task** ("learn the stack"), on the NUC with the robot up and standing still. The step gate is on, but the discovery itself reads only: it calls no skill that moves the robot.
2. The agent reads the stack repos in the workspace's `src/` (read-only) and the live graph (`ros_read` topics, `interface_show`), and writes:
   - a contract for each interface it can use, at `experimental`, with a description, inputs, outputs, preconditions, a timeout and the device;
   - a page per package: what it does, its interfaces, the frames and units.
3. `write_skill` (built: `agent_cli write-skill FILE`, `skill_writer.py`) checks each contract (the schema, the deny list, its references, its ROS types and fields, as `contract_check.py`), and saves it in the agent's folder with a mark on the first line (`AGENT_MARK`). It then loads through `Registry.refresh`, the one way in while the nodes run: the executor (at once after a write, and every 5 s) and `registry_node` (every 5 s) load new and changed files, each checked by the deny list and its references (and its ROS types, in the executor). A file with the mark always loads at maturity `untested`, so the agent does not grade its own skills even when it writes the file directly; a file's name must be its skill's name. A new version moves the old file to `old/`. The agent cannot replace a contract that a person wrote (no mark). It is on by default, because the library starts empty; `write_skill:=false` locks it, so only a person can add a contract. The contracts are always `<agent_dir>/contracts` (`agent_dir` defaults to `$EIGEN_VECTOR_WS/agent`).
   - `agent_cli interface NAME|TYPE` (built, read-only): the types of a topic, service or action in the live graph, or of one type, with their fields.
4. The operator reviews the seeded library before the first robot use, as the graduation gate says.

## Improving the skills

- Every task records which skill ran, for which subgoal, and how it ended (`steps.jsonl`), so graduation counts per skill and version.
- After a failure, the agent can write a new version of a skill (a precondition, a timeout, a check); its counts start again.
- Subgoals that work again and again become compositions (`docs/skill-packages.md`, "Compositions").

## Order of work (on this branch)

1. Task desk and `agent_cli` task commands (`docs/task-mode.md`): needed for every agent task. Done.
2. The deny list and the step gate on the robot. Done.
3. `write_skill`, `interface_show`, and the registry loading contracts while it runs. Done.
4. The agent runbook, the session's permissions, and the discovery task.

## Open questions

- How long may discovery take, and how many contracts is too many (one per interface, or only the ones a task can use)?
