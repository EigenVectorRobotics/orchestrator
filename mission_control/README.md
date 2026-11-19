# Mission Control

A web UI to see and drive the orchestrator: robot state, task, plan, step gate, trace, camera, map, skill graph, skill proposals, knowledge and runs.

Two pages live here and in the package:

- **The live page**: `orchestrator/web/index.html`, served by the adapter node (`orchestrator/orchestrator/mission_control_node.py`). It has the Mission tab of `v2.html`. It sends a mission, a step decision and a cancel.
- **The design**: `mockup/v2.html`, sample data, opened straight in a browser. The live page is built from it, one tab at a time:
  - **Mission**: the "Now" band shows what needs the operator. That is a step to approve or refuse, the running step, or the mission box. The page has no keyboard shortcuts: each decision is a click on its button, because a step may move the robot. Below it: the plan as a timeline, the camera with the EdgeTAM mask outlines and a small map, and the agent feed. The raw trace is in a closed panel.
  - **Skills**: a review queue for the skills that the agent wrote. **Keep** removes the agent's mark lines. The operator commits the change in git by hand. **Remove** moves the skill folder out of the library.
  - **Knowledge**: first the grades to commit, the beliefs ready to become facts, and the claims contradicted lately. Then all claims. The page approves a fact with the page token. The log records "operator via Mission Control".
  - **Runs**: each run with its steps to scale, its plan and what it left (skills, claims, grades). Also its bundle and its code versions.
  - The page escapes each value from the adapter once, where it comes in. The agent writes much of that text, and the page holds the operator's token.
- **The first mockup**: `mockup/index.html`. Its "Feature sets" tab lists every feature and the ROS interface behind it.

What the live Mission tab gets from the adapter:

- **Plan:** the plan, and each step of the open mission, from the trace: run, ended or refused. A refused step names its subgoal (the `step_gate` event), so the plan shows it.
- **Camera masks:** the adapter subscribes to `/track_3d/mask_outlines` only while the page shows the masks, because track_3d works on the outlines only while something subscribes.
- **Agent feed:** the trace events as short lines. It does not have the agent's own reasoning yet: that is in the session log of Codex or Claude Code, which the adapter does not read.

- **Skills tab:** the adapter reads the library files (`skill_registry/skill_review.py`). Keep and Remove need the page token, and they work only on a skill that the agent wrote. Keep takes the mark lines away; the skill stays untested, and the agent may no longer replace it. Remove moves the folder to `$EIGEN_VECTOR_WS/agent/removed_skills/`; it refuses a skill that another skill names. The executor's registry sees each change at its next refresh (5 s). A removed skill unloads when its folder is missing at two refreshes in a row, and not while the whole library is missing or empty (a git checkout, a link made again). Git is the operator's own step. Not shown yet: whether a skill is stale or live (only the executor knows).

- **Knowledge tab:** the adapter reads the store (`orchestrator/knowledge.py`) with its file lock, and applies expiry and code moves before it shows a tier (at most every 30 s), as the `knowledge` CLI does. Approve, retire and seen need the page token; with the token off they are refused, because any process on the NUC could then act as the operator. The log records each act with `by: operator` and `via: mission_control`. The staged grades are shown, not committed: the agent commits them (`agent_cli commit`), or the next mission does, because the agent may grade until then. "Contradicted lately" lists the live claims that a contradiction moved down in the last 14 days, until you mark them seen or retire them.
- **The token:** the node writes it to its log, which the agent's shell on the NUC can read. The token stops mistakes, not an agent that looks for it.
- **Runs tab:** the adapter reads the recorder's bundles in `~/orch_runs` (`orchestrator/run_browser.py`, parameter `runs_root`). It lists the newest 200 runs. For one run, it shows the steps, the plan, the refusals and what the run left. These come from the session's `events.jsonl`. It also shows the bag, the scratch files and the code versions. A run without an end in an old or closed session shows as "recorder stopped". A run is named by its key, `<session>/<folder>`. The adapter looks up the key in its list: a key is never a path.

Still to build in the live page: the small map, the stale and live state of a skill, and the agent's own reasoning in the feed.

## How it runs

One adapter node on the NUC (rclpy and a small web server in one process), started with `mission_control:=true` on the orchestrator launch file. It collects the state from the topics and serves the page on `127.0.0.1:8080`. It sends commands on the same topics as `agent_cli`, so the step gate, the limits and the recorder see no difference. Approve and Refuse publish on `/orchestrator/step_decision` with no tty check, and the gate stays the only judge of a decision. From the laptop:

```bash
ssh -L 8080:localhost:8080 vector@vector-nuc     # then open the link the node logs
```

The node logs `http://127.0.0.1:8080/?t=<token>` at start. Open that link, with its token: the page keeps the token for the tab, and a command without it is refused, because the agent's own session has a shell on the same NUC. Reading the page needs no token. `mission_control_token:=off` takes the check away.

The colors come from the team's "Standards and Regulations" deck: forest `#003310`, lime `#E0F0B0`. The fonts in `v2.html` and the live page are IBM Plex Sans and IBM Plex Mono. The first mockup uses Neuton, Open Sans and Roboto Mono.
