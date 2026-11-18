# orchestrator/planning

A task sentence becomes a tree. The planner asks an LLM, validates its answer and asks again with the errors.

- **`planner.py`**: The prompt from contracts, places and the tree library, and the validate-and-retry loop.
- **`backends.py`**: Claude, OpenAI-compatible and scripted backends.
- **`external.py`**: A backend answered by a Claude Code session through planner_cli.
- **`cli.py`**: planner_cli: watch the planner's open call and answer it.
- **`node.py`**: The planner node: runs preflight first, then plans and sends the tree.
