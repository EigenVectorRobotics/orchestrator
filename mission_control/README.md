# Mission Control

A web UI to see and drive the orchestrator: robot state, task, plan, step gate, trace, camera, map, skill graph, skill proposals, knowledge and runs.

Two pages live here and in the package:

- **The live page**: `orchestrator/web/index.html`, served by the adapter node (`orchestrator/orchestrator/mission_control_node.py`). It shows the executor's state, the open task, the plan, the step gate and the trace, and it sends a task, a step decision and a cancel.
- **The design reference**: `mockup/index.html`, fake data, opened straight in a browser. Its "Feature sets" tab lists every feature, the ROS interface behind it, and what is missing. The panels not yet in the live page come from here.

Still to build: camera, map, the decision turns, the skill graph and proposals, knowledge, and past runs. Those need the adapter to read the run folders, the knowledge store and the agent's contracts.

## How it runs

One adapter node on the NUC (rclpy and a small web server in one process), started with `mission_control:=true` on the orchestrator launch file. It collects the state from the topics and serves the page on `127.0.0.1:8080`. It sends commands on the same topics as `agent_cli`, so the step gate, the limits and the recorder see no difference. Approve and Refuse publish on `/orchestrator/step_decision` with no tty check, and the gate stays the only judge of a decision. From the laptop:

```bash
ssh -L 8080:localhost:8080 vector@vector-nuc     # then open the link the node logs
```

The node logs `http://127.0.0.1:8080/?t=<token>` at start. Open that link, with its token: the page keeps the token for the tab, and a command without it is refused, because the agent's own session has a shell on the same NUC. Reading the page needs no token. `mission_control_token:=off` takes the check away.

The theme comes from the team's "Standards and Regulations" deck: forest `#003310`, lime `#E0F0B0`, Neuton titles, Open Sans text, Roboto Mono labels.
