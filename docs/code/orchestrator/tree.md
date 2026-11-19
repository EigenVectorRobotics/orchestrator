# orchestrator/tree

A small BehaviorTree.CPP v4 interpreter: the same XML as the C++ library, run from Python.

- **`engine.py`**: Control nodes, decorators, the blackboard and the record of what ran.
- **`runner.py`**: Runs one tree at a time: builds it, starts it in place of the running one, ticks, cancels and traces it.
- **`leaf.py`**: The skill leaf and the agent leaf, and the factory that builds each leaf of a tree.
