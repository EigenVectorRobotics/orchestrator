# orchestrator/agent

One decision of a tree, handed to a model or the operator. Nothing here names a skill: each agent skill brings its spec, evidence and checks from its folder.

- **`api.py`**: The `AgentSkill` base class a skill's agent.py subclasses, and the call flow in a thread: evidence, rules, the backend, then the decision checks.
- **`link.py`**: The TCP link to the bridge on the laptop, with reconnects, and the push backend.
- **`protocol.py`**: The framing and protocol number, and the loaders for each skill's spec.py and prompt.py. Standard library only, so the bridge imports it.
