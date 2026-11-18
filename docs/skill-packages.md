# Skill package and skill graduation (design, not built)

Status: draft for review, 2026-11-05. Nothing in this note is built yet. Changed on 2026-11-05: one skill package on the NUC, not one package per device (see "Per-device packages (deferred)").

## Goal

- All skills are in one package on the NUC: `skill_registry`. The agent writes new skills into it.
- The validator and the executor do not change: they still read `/skills/live` and the contracts in the registry.

## The package

- `skill_registry` holds every skill: one contract YAML (the schema of `skill_registry/contract.py`) and, when needed, adapter code and documents (`docs/skill-structure.md`).
- `device` in the contract stays. It tells where the ROS interface runs (for example `thor` for the VLM questions, `orin` for locomotion), not where the skill files are.
- An adapter node runs on the NUC and calls the ROS interfaces on the other devices over the network. No skill files go to Orin or Thor.

## Live skills

- No new protocol. The registry does as now: it loads the contracts from the package, checks the ROS graph each second (services, topic publishers and subscribers, actions) and the VLM `/models` URL, and publishes `/skills/live`.
- A skill is live when its interface is on the graph. A device that goes down takes its interfaces off the graph, so its skills go off `/skills/live`.
- New skills load while the registry runs (no restart): the registry reloads a contract when its file changes.
- **Contract changes:** use the new contract, and log the change. (Now `Registry.announce` keeps the first contract and ignores changes.)
- **Bad contract:** refuse that skill only, and log it. The other skills stay live.

## Agent-written skills

### Where the agent runs

- The agent runs on the NUC and writes into the package there. It does not need a shell on Orin or Thor.

### What the agent can use

- Any ROS interface that the stack exposes: topics, services and actions. It can use a ROS interface that is not a skill yet (for example a controller's command topic), so it is not limited to the current skill contracts.
- **Never** the Unitree low-level layer: no DDS `rt/*` topics (`rt/lowcmd`, `rt/lowstate`, ...) and no Unitree SDK calls (`unitree_sdk2`, `unitree_sdk2py`). These go around the stack's safety layers (`unitree_interface` modes, joint limits, the arm planner).
- Enforced in two places, not only in the prompt:
  - The contract check refuses an interface name that starts with `rt/`, and any transport other than ROS action, service, topic, watch or VLM.
  - The registry refuses to load a skill whose adapter code imports `unitree_sdk2py`, links `unitree_sdk2`, or makes a raw DDS participant (`cyclonedds`, `fastdds` APIs).

### Versions

- Each contract has `version` (semver) and the registry adds `hash` (hash of the contract and adapter files).
- A change to the contract or the adapter is a new version. The use counts start again for the new version.
- The trace records `name@version` for each skill call, so a failure is linked to the exact version.

### Graduation

A skill goes up in maturity as it is used more and is reliable.

| Maturity | Planner | Real robot | How it gets here |
|---|---|---|---|
| `experimental` | sim only | no | new skill or new version |
| `wip` | yes, shown after `tested` skills | yes, in a tree with a fallback | sim: n ≥ 5 and reliability ≥ 0.6, then a human approves the first robot use |
| `tested` | yes, shown first | yes | robot: n ≥ 10 and reliability ≥ 0.8 |
| `deprecated` | hidden | no | n ≥ 10 and reliability ≤ 0.2, or a human sets it |

- **Reliability** is the Wilson lower bound of the success rate, so a skill does not go up after a small number of lucky successes. The numbers in the table are a first guess for review.
- **A tested skill goes down** to `wip` when its reliability over the last 10 uses is less than the limit.
- **Sim and robot counts are separate.** Success in the sim does not make a skill `tested`.
- **Success is not the skill's own report.** A use counts as a success only if the contract's `check` condition (another skill, for example a VLM question that the button is lit) confirms it, or a human marks it. Example: `PressController` reports success when the finger did not touch the button.
- **Where the counts live:** one stats file in the package (`stats.yaml`: name, version, sim and robot counts), written by the executor. `/skills/live` carries the maturity.
- The current maturity values (`tested`, `wip`, `untested`) map to this table, with `untested` renamed `experimental`, and a new `deprecated`.

### Compositions: behavior trees that graduate

A behavior tree is a composition that the agent makes from steps that worked, and it graduates like any other skill. A code skill is a composition too, and is built now.

- **One task is one run.** For a new task, the agent works step by step: it calls a skill, looks at the result, and selects the next one. It can call many skills in one task. The executor runs each call, so the contracts, the validator, the recorder and the `rt/*` ban still apply. The recorder makes one bundle (one bag) per task, not per tree.
- **The agent makes the tree.** When the same steps work in more than one run for one kind of task, the agent writes them as a BT (with the checks and fallbacks it saw were necessary) and a contract for it, from the run bundles. The tree is then a compound skill, at `experimental`.
- **It graduates with the same rules** as the table above: the same counts, the same `check`, the same human gate before the first robot use.
- **The task agent calls a graduated tree as one skill**, for example `call_elevator`.
- **Blind segments need a tree.** In the cabin there is no cloud, so the agent cannot work step by step there. Only a tree at `wip` or `tested` can run a blind segment.

Experiment: can the agent find the compositions alone? For the test, the agent must not see trees or procedures that people wrote. Keep them outside the agent's view (the session's permissions, the code sandbox), to compare with what the agent makes.

Built: task mode and one run per task (`docs/task-mode.md`), code skills. Not built yet: a contract for a tree (`transport: tree`), and the agent's tool to write a tree from run bundles.

### Similar work

- **Voyager** (Minecraft, LLM agent): a skill library of code. A skill goes into the library only after a self-check that the task is done. Without the self-check, results were 73 % worse.
- **LRLL** (robot manipulation): an LLM turns experience into new skills (Python functions) and adds them to the robot's skill library.
- **Playful-RATs** (2026, Berkeley): the closest to this design. Skills start `experimental`, go to `verified` after ≥ 3 uses with a success rate ≥ 0.5, and go to `deprecated` after ≥ 10 uses with a success rate ≤ 0.2. Verified skills can go down. Reliability uses the Wilson lower bound. The planner sees verified skills first and does not see deprecated skills.
- **Systems survey of agentic skills** (2026): stages (develop, staging, production, deprecation, removal), semantic versions, sandboxes, code signing and per-skill permissions.

Differences in our design:
- These works judge success in the sim or with an LLM critic. We use a different skill (the `check`) or a human.
- We add a human gate before the first robot use, and the agent can never use the low-level layer.

Sources:
- Voyager: https://arxiv.org/abs/2305.16291
- LRLL: https://arxiv.org/abs/2406.18746
- Playful-RATs: https://arxiv.org/abs/2606.19419 (section 3 and appendix, skill reliability)
- Survey: https://arxiv.org/abs/2608.29596

## Per-device packages (deferred)

The first design had one package per device (`nuc_skills`, `orin_skills`, `thor_skills`), each with a skill host that announced its full contracts on a volatile `/skills/announce` every 1 s, and a `sync_to_device` tool to copy agent-written skills to a device. It is deferred: the NUC reaches every interface over ROS, so one package does the same job with less to build and test.

The first reason for per-device packages was GPU work: an agent-written skill that needs the GPU on Thor (or Orin). The NUC has no GPU. For now, GPU work goes through interfaces that people deploy on Thor (the VLM server, `ground_server`), and the agent calls them over ROS or HTTP.

Look at it again only if one of these occurs:
- the agent must write GPU code, and no deployed interface on Thor can do the job;
- a skill must run code on Orin or Thor (for example a tight control loop that cannot go over the network);
- the NUC cannot reach a device's interfaces over ROS;
- the NUC has not enough CPU for the adapter nodes.

## Open questions

- Who can approve a skill for the robot: any teammate, or only the owner of that part of the stack?
- Where does the trace go when there is no cloud link in the elevator: does the NUC keep the counts and sync them later?
