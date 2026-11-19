# orchestrator/execution

Running trees on the NUC. The executor validates a tree, builds its leaves, ticks it at 10 Hz and traces every step.

- **`node.py`**: The executor node: its parameters and topics, the agent backend, and the tree and skill runners it builds.
- **`tree_runner.py`**: One tree at a time, with no ROS: validation, the recorder and preflight gates, the tick and the trace.
- **`node_setup.py`**: What every node loads (contracts from the skill packages, places, essential devices), and the trace.
- **`send_tree.py`**: Send one tree file to the executor, without the planner.
