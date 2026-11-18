# orchestrator/skills

How the executor calls a skill. A call runs the skill's own code from its folder around the contract's transport.

- **`api.py`**: The `Skill` base class and the handles a call returns. A skill.py subclasses `Skill`.
- **`context.py`**: What transports and skills share: ROS clients, camera, TF, thread pools and shared state.
- **`loader.py`**: Finds skill packages and the code in each skill folder. Standard library only.
- **`runner.py`**: Starts every non-agent call: before, start or the transport, then wrap.
- **`handles.py`**: Handles for action, service and topic calls.
- **`messages.py`**: Builds goals, requests and messages from a contract and its inputs.
- **`watch.py`**: The latest messages on a topic, and how long a feedback value has held.
- **`transports/`**: One module per transport: action, service, topic, watch, compare, alive and command, vlm, python. `default_transports()` lists them.
