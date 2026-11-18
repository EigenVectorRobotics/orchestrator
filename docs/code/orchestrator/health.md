# orchestrator/health

What preflight uses to check and start the robot's machines. No ROS here: the transports pass the graph in.

- **`checks.py`**: Topics, services and URLs that must answer, and the fixed shell commands that start stacks.
- **`stale_nodes.py`**: Finds robot-control nodes that run twice and processes left outside the launch.
