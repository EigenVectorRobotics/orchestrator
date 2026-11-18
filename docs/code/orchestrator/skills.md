# orchestrator/skills

How the executor calls a skill. A call runs the skill's own code from its folder around the contract's transport.

- **`api.py`**: The `Skill` base class and the handles a call returns. A skill.py subclasses `Skill`.
- **`loader.py`**: Finds skill packages and the code in each skill folder. Standard library only.
- **`runtime.py`**: What transports and skills share (ROS clients, camera, TF, pools, shared state), and the runner that starts every non-agent call.
- **`transports.py`**: Message building, topic watches, the call handles, and every transport. `default_transports()` lists them.
