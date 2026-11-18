# orchestrator/tree

A small BehaviorTree.CPP v4 interpreter: the same XML as the C++ library, run from Python.

- **`engine.py`**: Control nodes, decorators, the blackboard and the record of what ran.
- **`leaf.py`**: The skill leaf: inputs from attributes and the blackboard, timeouts, outputs and records.
- **`factory.py`**: Builds each leaf of a tree: agent skills through the agent, the rest through the runner.
