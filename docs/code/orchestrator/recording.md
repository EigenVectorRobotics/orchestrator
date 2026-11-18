# orchestrator/recording

Every run kept on the NUC, so it can be replayed and reviewed. The recorder loads no executor module.

- **`recorder.py`**: One folder per run: provenance, events, agent calls, frames, outcome.
- **`node.py`**: The recorder node: subscriptions, the bag per run, and the disk check.
