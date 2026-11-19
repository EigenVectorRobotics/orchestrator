# orchestrator/skills

How the executor calls a skill. A call runs the skill's own code from its folder around the contract's transport.

- **`api.py`**: The `Skill` base class and the handles a call returns. A skill.py subclasses `Skill`.
- **`loader.py`**: Finds skill packages and the code in each skill folder. Standard library only.
- **`runtime.py`**: What drivers and skills share (ROS clients, camera, TF, pools, shared state), and the runner that owns it with the agent and starts every non-agent call.
- **`transports.py`**: Message building, topic watches, the call handles, and the driver of every transport. `default_drivers()` lists them.
