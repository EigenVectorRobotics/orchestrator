# Mission Control

A web UI to see and drive the orchestrator: robot state, task, plan, step gate, trace, camera, map, skill graph, skill proposals, knowledge and runs.

Two pages live here and in the package:

- **The live page**: `orchestrator/web/index.html`, served by the adapter node (`orchestrator/orchestrator/mission_control_node.py`). It has the Mission tab of `v2.html`. It sends a mission, a step decision and a cancel.
- **The design**: `mockup/v2.html`, sample data, opened straight in a browser. The live page is built from it, one tab at a time:
  - **Mission**: the "Now" band shows what needs the operator. That is a step to approve or refuse, the running step, or the mission box. The page has no keyboard shortcuts: each decision is a click on its button, because a step may move the robot. Below it: the plan as a timeline, the camera with the EdgeTAM mask outlines, the Map panel under it, and the agent feed. The raw trace is in a closed panel.
  - **Skills**: a review queue for the skills that the agent wrote. **Keep** removes the agent's mark lines. The operator commits the change in git by hand. **Remove** moves the skill folder out of the library.
  - **Knowledge**: first the grades to commit, the beliefs ready to become facts, and the claims contradicted lately. Then all claims. The page approves a fact with the page token. The log records "operator via Mission Control".
  - **Runs**: each run with its steps to scale, its plan and what it left (skills, claims, grades). Also its bundle and its code versions.
  - The page escapes each value from the adapter once, where it comes in. The agent writes much of that text, and the page holds the operator's token.
- **The first mockup**: `mockup/index.html`. Its "Feature sets" tab lists every feature and the ROS interface behind it.

What the live Mission tab gets from the adapter:

- **Plan:** the plan, and each step of the open mission, from the trace: run, ended or refused. A refused step names its subgoal (the `step_gate` event), so the plan shows it.
- **Camera masks:** the adapter subscribes to `/track_3d/mask_outlines` only while the page shows the masks, because track_3d works on the outlines only while something subscribes.
- **Agent feed:** the trace events as short lines, and what the agent thinks and says, in time order. The adapter reads the agent's words from its session logs (`orchestrator/session_log.py`), about once a second while a mission is open and for 5 s after it closes. It reads a session that started in `agent` or `agent_discovery` (parameter `agent_folders`), and its workers. It reads only the reasoning summaries and the agent's messages, never the commands, their output or the prompts. These words are not on `/orchestrator/trace`. The recorder reads them itself into each run's `words.jsonl` (`docs/recorder.md`).
  - **Codex:** logs under `~/.codex/sessions` (parameter `session_logs`), in the day folders from 7 days before the mission start, because a resumed session stays in the folder of its first day. Codex writes a reasoning summary only when it starts with `-c model_reasoning_summary="detailed"`; the feed says so when the summaries are empty.
  - **Claude Code:** logs under `~/.claude/projects/<folder name, each other character a ->` (parameter `claude_logs`), and the workers' logs under `<session>/subagents/`. Claude Code writes its thinking only with `showThinkingSummaries`; the agent folders' settings (`orchestrator/agent/claude_settings.json`) set it.
- **Start agent:** while a mission is open, the button starts the agent's session for it, in tmux on the NUC: `tmux -L task` in `agent`, or `tmux -L discovery` in `agent_discovery` for a discovery task (`orchestrator/agent_session.py`). One task is one session: a start replaces the agent of that kind that runs. Launch argument `agent` picks the tool (`claude`, the default, or `codex`; empty: no button), `agent_program` its program (default: the tool's name on the PATH of a login shell). The session runs with no permission prompts; the folder's deny rules still apply. The first start of Claude Code on a host asks two questions (trust the folder, bypass mode): attach with `tmux -L task attach -t task` and answer them once. It needs the page token.

- **Skills tab:** the adapter reads the library files (`skill_registry/skill_review.py`). Keep and Remove need the page token, and they work only on a skill that the agent wrote. Keep takes the mark lines away; the skill stays untested, and the agent may no longer replace it. Remove moves the folder to `$EIGEN_VECTOR_WS/agent/removed_skills/`; it refuses a skill that another skill names. The executor's registry sees each change at its next refresh (5 s). A removed skill unloads when its folder is missing at two refreshes in a row, and not while the whole library is missing or empty (a git checkout, a link made again). Git is the operator's own step. A skill is **stale** when the executor's stamps say so. The adapter reads them through the code clock that the executor saves (`code_clock.yaml`). It checks at most every 30 s, because each check runs git in every repo. With no clock, or a clock for another library, the page says that the stale state is not known. A skill is **live** when its interface is in the ROS graph now: the adapter reads `/skills/live` from `registry_node`. Without `registry_node`, the page does not say live or not live.

- **Knowledge tab:** the adapter reads the store (`orchestrator/knowledge.py`) with its file lock, and applies expiry and code moves before it shows a tier (at most every 30 s), as the `knowledge` CLI does. Approve, retire and seen need the page token; with the token off they are refused, because any process on the NUC could then act as the operator. The log records each act with `by: operator` and `via: mission_control`. The staged grades are shown, not committed: the agent commits them (`agent_cli commit`), or the next mission does, because the agent may grade until then. "Contradicted lately" lists the live claims that a contradiction moved down in the last 14 days, until you mark them seen or retire them.
- **The token:** the node writes it to its log, which the agent's shell on the NUC can read. The token stops mistakes, not an agent that looks for it.
- **Runs tab:** the adapter reads the recorder's bundles in `~/orch_runs` (`orchestrator/run_browser.py`, parameter `runs_root`). It lists the newest 200 runs. For one run, it shows the steps, the plan, the refusals and what the run left. These come from the session's `events.jsonl`. It also shows the bag, the scratch files and the code versions. A run without an end in an old or closed session shows as "recorder stopped". A run is named by its key, `<session>/<folder>`. The adapter looks up the key in its list: a key is never a path.

- **Map panel:** under the camera, to scale, in the map frame with +y up (not north).
  - The floor plan is the route planner's own terrain graph (`<map>.terrain.npz` beside the prior map): light where the robot can walk, darker where a cell costs more (near a wall, in a keep-out zone), red where it is blocked. So the panel also shows why a route is blocked. `floor_plan.py` makes one image for each floor, once for each map: the cells within 0.8 m of the floor's main surface. `map_info`'s places file (`Places.annotations_path`) names the map and the z range of each floor.
  - On top: the places of the floor (from `map_info`), the robot and its heading (`/odom_base_link`), the route (`/route_planner/path`), and the goal of a step that waits at the gate (an input that is a pose, or the name of a place). A label that would cover another is left out; zoom in to see it.
  - It shows the robot's floor (`/slam_elevatorlio/floor`, else the robot's z). The floor buttons show another floor. The mouse wheel zooms, and a drag moves the map; **follow robot** keeps the robot in the middle. A grid line is 5 m, and 1 m when you zoom in. A pose older than 3 s shows grey.

## How it runs

One adapter node on the NUC (rclpy and a small web server in one process), started with `mission_control:=true` on the orchestrator launch file. It collects the state from the topics and serves the page on `127.0.0.1:8080`. It sends commands on the same topics as `agent_cli`, so the step gate, the limits and the recorder see no difference. Approve and Refuse publish on `/orchestrator/step_decision` with no tty check, and the gate stays the only judge of a decision. From the laptop:

```bash
ssh -L 8080:localhost:8080 vector@vector-nuc     # then open the link the node logs
```

The node logs `http://127.0.0.1:8080/?t=<token>` at start. Open that link, with its token: the page keeps the token for the tab, and a command without it is refused, because the agent's own session has a shell on the same NUC. Reading the page needs no token. `mission_control_token:=off` takes the check away.

The colors come from the team's "Standards and Regulations" deck: forest `#003310`, lime `#E0F0B0`. The fonts in `v2.html` and the live page are IBM Plex Sans and IBM Plex Mono. The first mockup uses Neuton, Open Sans and Roboto Mono.
