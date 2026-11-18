# orchestrator/execution

Running trees on the NUC. The executor validates a tree, builds its leaves, ticks it at 10 Hz and traces every step.

- **`node.py`**: The executor node: the preflight and recorder gates, the skill context, runner and agent.
- **`node_setup.py`**: What every node loads: contracts from the skill packages, places, essential devices.
- **`send_tree.py`**: Send one tree file to the executor, without the planner.
- **`trace.py`**: Each event as JSON on the trace topic and as a Foxglove log line.
