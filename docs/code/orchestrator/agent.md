# orchestrator/agent

One decision of a tree, handed to a model or the operator. Nothing here names a skill: each agent skill brings its spec, evidence and checks from its folder.

- **`api.py`**: The `AgentSkill` base class and the call types; a skill's agent.py subclasses it.
- **`engine.py`**: The call flow in a thread: evidence, rules, the backend, then the decision checks. `AgentSession` holds one call.
- **`leaf.py`**: The tree leaf for agent skills. It records each decision and ends the tree on abort.
- **`link.py`**: The TCP link to the bridge on the laptop, with reconnects, and the push backend.
- **`protocol.py`**: The framing and protocol number, and the loaders for each skill's spec.py and prompt.py. Standard library only, so the bridge imports it.
