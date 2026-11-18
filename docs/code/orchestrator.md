# orchestrator (Python package)

The core that plans, runs and records trees. It knows no skill: skills come from skill packages such as g1_skills, through the loader in `skills/`.

- **`agent/`**: Runs agent skills: gathers evidence, applies a skill's rules, and sends the call to the laptop bridge. The protocol module is shared with the bridge.
- **`common/`**: Small helpers with no executor state: geometry, images, topic names, places, sites and file locations.
- **`execution/`**: The executor node, which validates and ticks trees, and the tools that feed it.
- **`health/`**: Checks that stacks are up, stack launch commands, and the stale process finder.
- **`knowledge/`**: The claims store and its command line.
- **`planning/`**: The planner node and its LLM backends: a task becomes a validated tree.
- **`recording/`**: The run recorder: one folder per run, with provenance, events, agent calls and a bag.
- **`skills/`**: The skill API, the runner that starts every call, and one module per transport.
- **`tree/`**: The BehaviorTree.CPP v4 interpreter, the skill leaf and the leaf factory.
- **`vlm/`**: The VLM client and the Cosmos server that answers it on the Thor.
- **`sensing.py`**: Streams many skills share: the head camera, its recent frames, and the one TF buffer.
- **`fake_robot.py`**: The robot's topics, services and actions, a camera, a tracker and a VLM server, for runs without the G1.
