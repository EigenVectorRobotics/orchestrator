---
name: discover
description: The discovery session of the G1 orchestrator. Read the stack's code and the live ROS graph, and write a skill contract for each interface that a task could use, and one doc page per package. Use when a discovery task is open ("Learn the stack", started with Mission Control's Start discovery), or when the operator asks you to run or continue discovery.
---

# Discover the stack

You are the discovery session: you write the skills that task sessions use. A task session tries them later, on
real missions. That is the test of your work.

## Rules

- **Never move the robot.** The desk refuses `agent_cli run` in a discovery task.
- **Skills go in only through `agent_cli write-skill`.** It checks each contract and loads it. Only you run it; a
  subagent writes drafts in the scratch folder.
- **The contract grammar in the open message is the authority** for every field, the code skills too.
- **Do not read `~/orchestrator_ws`.** It is another team's work.

## Steps

1. **Check your rules.** Run `agent_cli run --help`. A rule must reject it. If it runs, your session did not load its
   rules (for Codex: trust this folder, then start again): stop and tell the operator.
2. **Check the task.** Run `agent_cli task wait`. The task line must say `(discovery)`. If it does not, stop: that
   task is for a task session. If the message has no contract grammar, `write-skill` is off on this robot: stop and
   tell the operator. Save the open message once, for the workers: `agent_cli task show >
   <scratch>/task.md` (the scratch folder is in the message).
3. **Read the state.**
   - `progress.md` in this folder. If it does not exist, make it from the template at the end of this file.
   - `agent_cli skills` once: the skills that exist, and the stale ones with the reason. Keep a short list of their
     names and interfaces; do not read the catalog again for each draft.
   - `ls docs/`. Read a page only for the repo that you work on.
4. **Choose the work.** `ls $EIGEN_VECTOR_WS/src`. Skip `orchestrator` (the software that runs you). First the repos
   of stale skills, then the repos that `progress.md` does not mark done.
5. **One repo at a time.** If you can start subagents, give each repo to one subagent with
   [repo-worker.md](repo-worker.md), filled in. If you cannot, do the worker's steps yourself. The worker writes its
   drafts in `<scratch>/<repo>/` and returns only the file names, the facts and the questions.
6. **Write the skills.** For each draft:
   - If your list has a skill for the same interface, read that one contract and improve it, or drop the draft.
   - Run `agent_cli write-skill <scratch>/<repo>/<name>.yaml` (and `--code FILE` for a code skill).
   - A refusal names the rule. Fix the draft and try again, three times at most. Then record the refusal in
     `progress.md` and continue.
   - Add each skill that loads to your list.
7. **Write what you learned.**
   - Copy the package page to `docs/<package>.md`: what the package does, its interfaces, the frames, the units.
   - A measured fact (a rate, a frame, a unit that the code does not state) goes to the knowledge store:
     `knowledge note "TEXT" --by agent:discovery --run <run_id> --evidence <what you read> --about skill=<name>`.
8. **Update `progress.md`** after each repo: done, the skills written, the refusals and why, open questions.
9. **End.**
   - **Context nearly full:** write a short summary in `progress.md` and stop. Do not end the task: it has no time
     limit, and a new session continues it from step 1 (`agent_cli task wait` gives the same open task).
   - **All repos done:** run `agent_cli done --reason R`. If some repos cannot be done, `agent_cli give-up --reason
     R`, with what is left in `progress.md`.

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
- The live graph: `ros2 node list`, `ros2 node info NODE`, `ros2 topic info -v TOPIC` for the names and types;
  `agent_cli interface TYPE` once for each type whose fields you need; `agent_cli topic TOPIC` for one message;
  `agent_cli tf TARGET SOURCE`. `write-skill` checks the types and fields again, so do not check each interface
  beforehand.
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
