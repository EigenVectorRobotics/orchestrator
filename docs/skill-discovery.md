# Skill discovery: the agent seeds its own skill library

Status: 2026-11-18, the discovery session is built (not yet run on the robot). First draft 2026-11-05. Branch `feat/skill-discovery`. Related: `docs/task-mode.md`, `docs/skill-packages.md` (versions, graduation, compositions), `docs/knowledge.md`.

## Goal

- The agent finds its base skills itself: it reads the stack's code and the live ROS graph, and writes a contract for each interface that it can use. This seeds the skill library.
- The skills then get better with use: graduation counts, new versions after failures, compositions (BTs) from subgoals that work again and again.
- The test: compare what the agent finds and builds with skills that people wrote by hand.

## What the agent can use

**Everything that the stack exposes, except the deny list below.** That includes, for example:
- every ROS topic, service and action of the stack on the NUC, the Orin and Thor: navigation (`route_planner`, `local_planner`, the SLAM node), manipulation (`move_to_pose`, visual servoing), `map_info` (places, regions, floors, `NavigateToPlace`), track anything on the Orin, the camera stream on the Orin, the VLM interfaces on Thor (the HTTP server and `ground_server`), and the parts of `unitree_interface` that are not denied;
- the stack's code in the workspace's `src/` (read-only), to find out what the interfaces do;
- the orchestrator's own tools, through `agent_cli`: `plan`, `run`, `write-skill`, `interface` (the type of a topic, service or action), `say` (`/speak`), `done`, `give-up`, and the `knowledge` CLI;
- the transports that the executor knows (action, service, topic, watch, graph, compare, vlm, code), and `ask_vlm`, the orchestrator's one closed question to the VLM.

People write only a short agent runbook (`orchestrator/agent/runbook.md`, which `<agent_dir>/runbook.md` links to; the agent never writes it, it holds only facts that hold by construction, and what the robot or the team observed goes to the knowledge store instead): what the robot is, the devices, how to see that the stack is up, the safety rules and the deny list, the places file. It describes the robot and the rules, not the skills or the steps of a task. The agent writes the codebase docs itself during discovery (`<agent_dir>/docs/<package>.md`, a link to `orchestrator/agent/docs/` in git).

## What the agent cannot use

**Denied, enforced in code** (built: `skill_registry/skill_registry/deny.yaml`, the operator's file; `DenyList.check` runs when a contract is loaded, and will run in `write_skill`; a denied contract is left out with a warning; the agent sandbox blocks the listed modules):
- low-level control: every `rt/*` DDS topic (`rt/lowcmd`, `rt/lowstate`, `rt/arm_sdk`, `rt/dex3/*`, ...), any `lowcmd` topic, the Unitree SDK (`unitree_sdk2`, `unitree_sdk2py`);
- `/unitree_interface/change_mode` to `LOW_LEVEL` (4), `EMERGENCY` (3) or `MONOSTATE` (0): a contract for it may map only `idle` (1) and `high_level` (2);
- `/unitree_interface/ready_locomotion` (it damps the robot and stands it up again).

**The session too.** In this experiment the agent is a Claude Code session with a shell on the NUC. The checks above cover what goes through the executor and the sandbox; a shell can still call `ros2 service call` directly. So the session runs with its own permissions: shell commands only through `agent_cli` (without `step approve|refuse`) and `knowledge list|show|note|confirm|contradict` (not `approve|refuse|retire`, which are the operator's; `--by operator` works only at a terminal), and read-only tools. Built (2026-11-18): the deny rules in `orchestrator/agent/claude_settings.json` (README, "The task session"); the allow list is not built.

**No hand-written skills** (2026-11-09). The agent starts with zero skills; it can write `ask_vlm` itself, because the `vlm` transport is part of the executor and the `vlm` package is in `src/`. If it does not find it, we seed it.

**Hidden for the test** (out of the agent's view):
- for code skills, `~/orchestrator_ws`: the sandbox refuses to read or list it (`HIDDEN` in `sandbox.py`).
- How: the launch argument `skills_dir` defaults to `<agent_dir>/skills`, a link to `skill_registry/skills/` (empty at the start). The session's permissions keep its shell out of `~/orchestrator_ws`.
- Not hidden: this repo's source. It holds no skill and no tree that a person wrote. The agent's own skills and docs are in it (`skill_registry/skills/`, `orchestrator/agent/docs/`). A Claude Code deny rule also blocks a link into a denied folder. An allow rule cannot open a folder inside a denied folder. Thus, if we hide the source, we also hide the agent's skills and docs.

## Discovery

Discovery is its own session (decided 2026-11-18). It reads the stack's code and writes contracts. A task session uses only the contracts and does not read the code.

- **Start:** Mission Control's **Start discovery** button, or `ros2 topic pub --once /orchestrator/task std_msgs/msg/String '{data: "{\"task\": \"Learn the stack\", \"discovery\": true}"}'`. Then start a Claude Code or Codex session in `$EIGEN_VECTOR_WS/agent_discovery`.
- **A discovery task** goes through the desk like any task (a run, a bundle, `write-skill`), with its own limits (`DISCOVERY_LIMITS` in `task_desk.py`): no `run`, so discovery never moves the robot; no time limit; and no idle speech, because reading code takes long with no command between.
- **The procedure is an agent skill** in git: `orchestrator/agent/skills/discover/SKILL.md`, linked into the discovery folder for Claude Code (`.claude/skills/`) and for Codex (`.codex/skills/`). A change to the procedure is a git change for review.
- **The procedure** (scope, what gets a skill, one subagent per repo, `progress.md`, what it writes) is in the skill, not here. The decisions: all stack repos except the orchestrator, not one mission; a contract for each interface that a task could use, with no number limit and no duplicates; only the coordinator runs `write-skill`.
- **The test:** discovery checks the types and reads the live graph. A skill that moves the robot stays `untested`. A new task session that does real missions with only these skills is the verifier (as in ASPIRE).
- **When the stack changes:** the stamps mark skills stale; a later discovery does the repos of stale skills first.
- **Two folders, two permission sets.** `scripts/link_agent_dir.sh` makes both `.claude/settings.json` files from templates in git (`orchestrator/agent/claude_settings.json`, `claude_settings_discovery.json`, by `scripts/agent_settings.py`):
  - the task session (`$EIGEN_VECTOR_WS/agent`): a Read deny for each stack repo in `src/` by its absolute path (not the orchestrator repo: its skills and docs are there);
  - the discovery session (`$EIGEN_VECTOR_WS/agent_discovery`): it reads `src/`; `agent_cli run` and edits in the skills folder are denied.
  - Checked with live Claude Code sessions (2026-11-18).
- **Codex** reads `.codex/rules/agent.rules` in the folder where it starts. On the NUC, Codex is the only session tool. The file holds the same shell rules, made from the same templates. Checked on the NUC with `codex execpolicy check` and a live `codex exec` (2026-11-18).
  - **Codex gets no file rules.** It reads and writes files with shell commands. Thus a Codex task session can read the stack repos, and any Codex session can edit the skills folder, `deny.yaml`, `gate.yaml` and the runbook. `write-skill`, the executor's deny list and git review still apply.
  - Codex may use a folder's rules only when it trusts the folder. The `discover` skill checks this first: `agent_cli run --help` must be rejected. On the NUC the Codex sandbox (bubblewrap) cannot start, so start Codex with `-s danger-full-access`; the rules still apply. For the reasoning in the session log, add `-c model_reasoning_summary="detailed"`.

## Improving the skills

- Every task records which skill ran, for which subgoal, and how it ended (`steps.jsonl`), so graduation counts per skill and version.
- After a failure, the agent can write a new version of a skill (a precondition, a timeout, a check); its counts start again.
- Subgoals that work again and again become compositions (`docs/skill-packages.md`, "Compositions").

## Order of work (on this branch)

1. Task desk and `agent_cli` task commands (`docs/task-mode.md`): needed for every agent task. Done.
2. The deny list and the step gate on the robot. Done.
3. `write_skill`, `interface_show`, and the registry loading contracts while it runs. Done.
4. The agent runbook, the session's permissions, and the discovery task. Done (2026-11-18).

## Open questions

- How long does discovery take on the robot? No limit is set; measure it in the first run.
- How many contracts is too many? Answered (2026-11-18): one per interface that a task could use, with no duplicates; no number limit.
