# Mission Control

A web UI to see and drive the orchestrator: robot state, task, plan, step gate, trace, camera, map, skill graph, skill proposals, knowledge and runs.

Status: mockup only, with fake data. Nothing connects to the robot yet.

Open `mockup/index.html` in a browser. The "Feature sets" tab lists each feature, the ROS interface behind it, and what is missing.

## Plan

One adapter node on the NUC (rclpy and a small web server in one process). It collects the state from the topics, reads the run folders, the knowledge store and the agent's contracts, and serves the page on `127.0.0.1:8080`. It sends commands on the same topics as `agent_cli`, so the step gate, the limits and the recorder see no difference. Approve and Refuse on the page publish on `/orchestrator/step_decision`, with no tty check. From the laptop:

```bash
ssh -L 8080:localhost:8080 vector@vector-nuc     # then open http://localhost:8080
```

The theme comes from the team's "Standards and Regulations" deck: forest `#003310`, lime `#E0F0B0`, Neuton titles, Open Sans text, Roboto Mono labels.
