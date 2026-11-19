# orchestrator/execution

Running trees on the NUC. The executor validates a tree, builds its leaves, ticks it at 10 Hz and traces every step.

- **`node.py`**: The executor node: which trees may run (the recorder gate, validation, the preflight gate), its topics, the agent backend, and the tree and skill runners it builds.
- **`node_setup.py`**: What every node loads (contracts from the skill packages, places, essential devices), and the trace.
- **`send_tree.py`**: Send one tree file to the executor, without the planner.
