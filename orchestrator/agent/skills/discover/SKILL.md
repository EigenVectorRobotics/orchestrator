---
name: discover
description: The discovery session of the G1 orchestrator. Read the stack's code and the live ROS graph, and write a skill contract for each interface that a task could use, and one doc page per package. Use when a discovery task is open ("Learn the stack", started with Mission Control's Start discovery), or when the operator asks you to run or continue discovery.
---

# Discover the stack

You are the **discovery session**. You read the robot's software and write the skills that task sessions use. You
never move the robot. A task session tries your skills later, on real missions: that is the test of your work.

## Rules

- **Never move the robot.** The desk refuses `agent_cli run` in a discovery task, and your settings deny it.
- **Write skills only with `agent_cli write-skill`.** It checks each contract (the grammar, the deny list, the ROS
  types) and loads it. Do not write files in the skills folder yourself.
- **Do not edit** the runbook, `deny.yaml`, `gate.yaml` or your settings. They are the operator's.
- **Do not read `~/orchestrator_ws`.** It is another team's work.
- **One writer.** Only you (the coordinator) run `write-skill`. A subagent only reads and returns drafts.

## Steps

1. **Check the task.** Run `agent_cli task wait`. The task line must say `(discovery`. If it does not, stop: that
   task is for a task session. Keep the open message: it has the runbook (what the robot is) and the contract
   grammar (what `write-skill` takes). The grammar is the authority for every field.
2. **Read the state.**
   - `progress.md` in this folder. If it does not exist, make it from the template at the end of this file.
   - `agent_cli skills`: the skills that exist, and the stale ones with the reason.
   - `docs/`: the package pages that exist.
3. **Choose the work.** List the repos: `ls $EIGEN_VECTOR_WS/src`. Skip `orchestrator` (that is the software that
   runs you). Do first the repos of stale skills, then the repos that `progress.md` does not mark done.
4. **One repo at a time.** If you can start subagents, give each repo to one subagent with
   [repo-worker.md](repo-worker.md), filled in, with the contract grammar from the open message. If you cannot,
   do the worker's steps yourself. The worker returns a
   package page and contract drafts.
5. **Write the skills.** For each contract draft:
   - Compare it with `agent_cli skills`. If a skill does the same thing, improve that skill or drop the draft.
   - Save it in the task's scratch folder and run `agent_cli write-skill FILE` (a code skill: `--code FILE`).
   - A refusal names the rule. Fix the draft and try again, three times at most. Then record the refusal in
     `progress.md` and continue.
6. **Write what you learned.**
   - The package page goes to `docs/<package>.md`. It says what the package does, its interfaces, the frames and
     the units.
   - A measured fact (a rate, a frame, a unit that the code does not state) goes to the knowledge store:
     `knowledge note "TEXT" --by agent:discovery --run <run_id> --evidence <what you read> --about skill=<name>`.
7. **Update `progress.md`** after each repo: done, the skills written, the refusals and why, open questions.
8. **End.** When all repos are done, or your context is nearly full, write a short summary in `progress.md` and run
   `agent_cli done --reason R`. A new session continues from `progress.md`.

## What gets a skill

- **Yes:** an interface that a task could call or read. For example: an action server (navigate, move the arm), a
  service that changes or reports a state, a topic that a node listens to for a command, a state or sensor topic
  that a task must check (the pose, a door state, a detection).
- **No:** internal plumbing (`/rosout`, `/parameter_events`, `/tf_static`, `~/transition_event`), debug and
  visualization topics (markers, images for RViz), the internal topics between the nodes of one package, and an
  interface that a skill already covers.
- **Never:** anything on the deny list in the runbook (low-level control, `rt/*`, the Unitree SDK, the denied
  `unitree_interface` modes). `write-skill` refuses it.
- **No limit on the number.** Quality is the limit: each skill passes `write-skill`, and there are no duplicates.

## How to look at the robot (read only)

- The code: read `$EIGEN_VECTOR_WS/src/<repo>` with your file tools.
- The live graph: `agent_cli topics`, `agent_cli interface NAME|TYPE`, `agent_cli topic TOPIC` (one message),
  `agent_cli tf TARGET SOURCE`, `ros2 node list`, `ros2 node info NODE`, `ros2 topic info -v TOPIC`.
- An interface that is in the code but not on the graph: its node or its device is not up. Write the skill from
  the code, and say so in `progress.md`. Do not conclude that it does not exist.

## Template: progress.md

```markdown
# Discovery progress

## Repos
| Repo | State | Skills written | Refused (why) | Notes |
|---|---|---|---|---|

## Open questions

## Sessions
- <date> <run_id>: what this session did
```
