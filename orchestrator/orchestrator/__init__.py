"""The core that plans, runs and records trees. It knows no skill: skills come from skill packages such as g1_skills,
through the loader in skills/.

- `agent/`: one decision of a tree, handed to a model or the operator through the bridge on the laptop
- `common/`: small helpers with no executor state: geometry, images, topic names, places, sites and file locations
- `execution/`: the executor node, which validates and ticks trees, and the tools that feed it
- `health/`: checks that stacks are up, stack launch commands, and the stale process finder
- `planning/`: the planner node and its LLM backends: a task becomes a validated tree
- `recording/`: the run recorder: one folder per run, with provenance, events, agent calls and a bag
- `skills/`: the skill API, the runner that starts every call, and the transports
- `tree/`: the BehaviorTree.CPP v4 interpreter, the skill leaf and the leaf factory
- `vlm/`: the VLM client and the server that answers it on the Thor
- `sensing.py`: what many skills share: the head camera, its recent frames, and the one TF buffer
- `fake_robot.py`: the robot's topics, services and action, a camera, a tracker and a VLM server, for runs without the G1
"""
