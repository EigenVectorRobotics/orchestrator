# The skill library

One folder for each skill:

```
<name>/
  contract.yaml   what the skill is (docs/skill-structure.md)
  code.py         for a code skill (transport: code): def run(skills, inputs)
  old/            earlier versions; write_skill keeps them; not in git
```

The executor and the registry read this folder through the workspace link `$EIGEN_VECTOR_WS/agent/skills` (`scripts/link_agent_dir.sh`), not through the colcon install. So a skill that `write_skill` saves loads at once, with no build, and shows here as a change in git.

The agent writes skills with `write_skill`. A file that starts with the agent's mark loads at maturity `untested`. A person reviews the change and commits it. A person may also write a skill folder here by hand.
