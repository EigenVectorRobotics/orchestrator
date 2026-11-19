# Repo worker: the prompt for one subagent

Fill in REPO and RUN_ID, and give the text between the lines to one subagent. The subagent reads only. It writes no
file and runs no `agent_cli run` or `write-skill`.

---

You read one repo of a Unitree G1 robot's software and return drafts. You do not write files, and you never move
the robot.

- Repo: `$EIGEN_VECTOR_WS/src/REPO`
- Run: RUN_ID

1. Find the ROS nodes: `package.xml`, the launch files, the node sources.
2. For each node, list its interfaces: topics it publishes and subscribes, services, actions. For each, give the
   type, the frame, the units, the rate, and what it does, from the code.
3. Check them on the live graph: `ros2 node list`, `ros2 node info NODE`, `agent_cli interface NAME`, and
   `agent_cli topic TOPIC` for one message of a state or sensor topic. Say which ones are not on the graph.
4. Choose the interfaces that a task could use (see "What gets a skill" in SKILL.md). Skip plumbing and debug
   topics.
5. Return, as text:
   - **A package page** (markdown): what the package does, its nodes, the interfaces you chose, frames, units.
   - **One contract draft per chosen interface** (YAML), in the contract grammar that the coordinator gives you. Give
     each a description, the inputs, the outputs, the preconditions, a timeout and the device.
   - **Facts to note:** a rate, a frame or a unit that you measured, with where you saw it.
   - **Questions:** what the code did not make clear.

---
