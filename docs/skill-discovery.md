# Skill discovery: the agent seeds its own skill library (design, not built)

Status: draft for review, 2026-11-05. Branch `feat/skill-discovery`. Related: `docs/task-mode.md`, `docs/skill-packages.md` (versions, graduation, compositions), `docs/knowledge.md`.

## Goal

- The agent finds its base skills itself: it reads the stack's code and the live ROS graph, and writes a contract for each interface that it can use. This seeds the skill library.
- The skills then get better with use: graduation counts, new versions after failures, compositions (BTs) from subgoals that work again and again.
- The test: compare what the agent finds and builds with the skills that people wrote (the 32 contracts, the trees, `manipulation_recovery.md`).

## What the agent can use

**Everything that the stack exposes, except the deny list below.** That includes, for example:
- every ROS topic, service and action of the stack on the NUC, the Orin and Thor: navigation (`route_planner`, `local_planner`, the SLAM node), manipulation (`move_to_pose`, visual servoing), `map_info` (places, regions, floors, `NavigateToPlace`), track anything on the Orin, the camera stream on the Orin, the VLM interfaces on Thor (the HTTP server and `ground_server`), and the parts of `unitree_interface` that are not denied;
- the stack's code in `eigen_vector_ws` (read-only), to find out what the interfaces do;
- the orchestrator's own tools: `plan`, `run_skill`, `note` (the knowledge store), `say` (`/speak`), `read_file`, `ros_read`, `get_image`, `run_python`, `done`, `give_up`, and new: `write_skill`, `interface_show` (the type of a topic, service or action);
- the transports that the executor knows (action, service, topic, watch, graph, compare, vlm, press_tip, agent), and `ask_vlm`, the orchestrator's one closed question to the VLM.

People write only a short agent runbook (`docs/agent/runbook.md`): what the robot is, the devices, how to see that the stack is up, the safety rules and the deny list, the places file. It describes the robot and the rules, not the skills or the steps of a task. The agent writes the codebase docs itself during discovery (`~/orch_skills/docs/<package>.md`).

## What the agent cannot use

**Denied, enforced in code** (a deny list in the config, checked when a contract is loaded or written, and by the sandbox for adapter code):
- low-level control: every `rt/*` DDS topic (`rt/lowcmd`, `rt/lowstate`, `rt/arm_sdk`, `rt/dex3/*`, ...), any `lowcmd` topic, the Unitree SDK (`unitree_sdk2`, `unitree_sdk2py`);
- `/unitree_interface/change_mode` to `LOW_LEVEL` (4), `EMERGENCY` (3) or `MONOSTATE` (0): a contract for it may map only `idle` (1) and `high_level` (2);
- `/unitree_interface/ready_locomotion` (it damps the robot and stands it up again).

**Hidden for the test** (kept in the repo for the comparison, out of the agent's view):
- the hand-written skills: all 32 contracts except `ask_vlm` (so also `door_open` and `button_lit`, which are `ask_vlm` with a fixed question), the trees, the agent skills `manipulation_recovery.md` and `press_judge.md`, and the experience log;
- the orchestrator's source and `skill_registry/`, because they hold the above;
- `docs/RUNBOOK.md` (Tingxuan's), because it gives the steps of the elevator task.
- How: the launch argument `contracts_directory` points at the agent's package (`~/orch_skills/`, which starts with `ask_vlm` only), and `agent_read_roots` leave out the hidden paths.

## Discovery

1. **A discovery task** ("learn the stack"), on the NUC with the robot up and standing still. The step gate is on, but the discovery itself reads only: it calls no skill that moves the robot.
2. The agent reads the stack repos in `eigen_vector_ws` (read-only) and the live graph (`ros_read` topics, `interface_show`), and writes:
   - a contract for each interface it can use, at `experimental`, with a description, inputs, outputs, preconditions, a timeout and the device;
   - a page per package: what it does, its interfaces, the frames and units.
3. `write_skill` validates each contract (schema, deny list, the ROS type exists) and saves it in `~/orch_skills/contracts/`; the registry loads it while it runs.
4. The operator reviews the seeded library before the first robot use, as the graduation gate says.

## Improving the skills

- Every task records which skill ran, for which subgoal, and how it ended (`steps.jsonl`), so graduation counts per skill and version.
- After a failure, the agent can write a new version of a skill (a precondition, a timeout, a check); its counts start again.
- Subgoals that work again and again become compositions (`docs/skill-packages.md`, "Compositions").

## Order of work (on this branch)

1. Task desk and `agent_cli` task commands (`docs/task-mode.md`): needed for every agent task.
2. `write_skill`, `interface_show`, the deny list, and the registry loading contracts while it runs.
3. The agent runbook, and the discovery task.
4. The step gate on the robot.

## Open questions

- How long may discovery take, and how many contracts is too many (one per interface, or only the ones a task can use)?
