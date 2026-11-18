# orchestrator/tree

A small BehaviorTree.CPP v4 interpreter: the same XML as the C++ library, run from Python.

- **`engine.py`**: Control nodes, decorators, the blackboard and the record of what ran.
- **`leaf.py`**: The skill leaf and the agent leaf, and the factory that builds each leaf of a tree.
