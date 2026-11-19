# Mission Control

A web UI to see and drive the orchestrator: robot state, task, plan, step gate, trace, camera, map, skill graph, skill proposals, knowledge and runs.

Two pages live here and in the package:

- **The live page**: `orchestrator/web/index.html`, served by the adapter node (`orchestrator/orchestrator/mission_control_node.py`). It shows the executor's state, the open task, the plan, the step gate and the trace, and it sends a task, a step decision and a cancel.
- **The design**: `mockup/v2.html`, sample data, opened straight in a browser. The live page is built from it, one tab at a time:
  - **Mission**: the "Now" band shows what needs the operator. That is a step to approve or refuse (keys A and R), the running step, or the mission box. Below it: the plan as a timeline, the camera with the EdgeTAM mask outlines and a small map, and the agent feed. The raw trace is in a closed panel.
  - **Skills**: a review queue for the skills that the agent wrote. **Keep** removes the agent's mark line. The operator commits the change in git by hand. **Remove** deletes the skill folder.
  - **Knowledge**: first the grades to commit, the beliefs ready to become facts, and the claims contradicted lately. Then all claims. The page approves a fact with the page token. The log records "operator via Mission Control".
  - **Runs**: each run with its steps to scale, its plan and what it left (skills, claims, grades). Also its bundle and its code versions.
  - The page escapes each value from the adapter once, where it comes in. The agent writes much of that text, and the page holds the operator's token.
- **The first mockup**: `mockup/index.html`. Its "Feature sets" tab lists every feature and the ROS interface behind it.

Still to build in the live page: everything in `v2.html` that `index.html` (the live page) does not have. The Skills, Knowledge and Runs tabs need the adapter to read the skill library, the knowledge store and the run folders. The camera masks need the adapter to read `/track_3d/mask_outlines`.

## How it runs

One adapter node on the NUC (rclpy and a small web server in one process), started with `mission_control:=true` on the orchestrator launch file. It collects the state from the topics and serves the page on `127.0.0.1:8080`. It sends commands on the same topics as `agent_cli`, so the step gate, the limits and the recorder see no difference. Approve and Refuse publish on `/orchestrator/step_decision` with no tty check, and the gate stays the only judge of a decision. From the laptop:

```bash
ssh -L 8080:localhost:8080 vector@vector-nuc     # then open the link the node logs
```

The node logs `http://127.0.0.1:8080/?t=<token>` at start. Open that link, with its token: the page keeps the token for the tab, and a command without it is refused, because the agent's own session has a shell on the same NUC. Reading the page needs no token. `mission_control_token:=off` takes the check away.

The colors come from the team's "Standards and Regulations" deck: forest `#003310`, lime `#E0F0B0`. The fonts in `v2.html` are IBM Plex Sans and IBM Plex Mono. The first mockup and the live page still use Neuton, Open Sans and Roboto Mono.
