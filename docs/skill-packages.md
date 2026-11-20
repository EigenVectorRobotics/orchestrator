# Skill package and skill graduation (design, partly built)

Status: draft for review, 2026-11-05. Changed on 2026-11-05: one skill package on the NUC, not one package per device (see "Per-device packages (deferred)"). Changed on 2026-11-19: why compositions exist ("Where the agent decides"), and a skill's evidence is the agent's grade of each step ("Credit assignment"). Built since: one package, live skills, agent-written skills, code skills, the grades of steps, graduation (computed, shown), tree skills. Changed on 2026-11-20: no tool only for trees; the agent writes a tree like any skill.

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
- **Contract changes:** use the new contract, and log the change. (There is no announcement: the registry reads the contracts from the library folder.)
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

- Each contract has `version` (semver), and the stamps keep a digest of the skill's files (`skill_stamps.py`).
- A change to the contract or the code is a new version. The use counts start again for the new version.
- The trace records `name@version` for each skill call, so a failure is linked to the exact version.

### Where the agent decides

This is the reason for graduation and compositions.

- The agent decides after a step: it looks at the result, then recovers, changes the plan or goes on. It must attempt a step before it can decide about it.
- A step is always one skill. That skill can be a one-off skill, a code skill, or a tree. To the agent, all three are one skill.
- **A composition that only calls skills is a tree, not code** (decided 2026-11-19). A tree has no code to get wrong: it only orders skills, checks and fallbacks. When a composition needs other code between the skills (for example a computation from one result to the next input), it is a code skill.
- An LLM cannot write a good tree in one attempt. Its parts may not be reliable, and the agent must see each result to find out. So trust is earned from the bottom up:
  1. One-off skills graduate when they work again and again.
  2. Graduated skills are composed into trees. A tree holds its own checks and fallbacks, so the agent does not decide inside it.
  3. Trees that graduate are composed into bigger trees.
- As trust grows, the agent calls bigger skills. It then decides after a tree, not after each part.
- In some places the agent cannot decide at all. In the elevator cabin the network is bad or gone. There, the step must be one trusted tree with its own recovery ("Compositions").

### Graduation

A skill goes up in maturity as it is used more and is reliable.

| Maturity | Planner | Real robot | How it gets here |
|---|---|---|---|
| `untested` | yes, shown last | yes, a person approves each step at the gate | new skill or new version |
| `wip` | yes, shown after `tested` skills | yes, in a tree with a fallback | robot: n ≥ 5 and reliability ≥ 0.6 |
| `tested` | yes, shown first | yes | robot: n ≥ 10, reliability ≥ 0.8, and no failure in the last 10 |
| `deprecated` | hidden | no | robot: n ≥ 10 and the upper bound ≤ 0.2; earned only, a contract cannot say it |

The planner order and the robot column are the plan for when maturity is used at run time ("Not yet" below).

- **Reliability** is the Wilson lower bound of the success rate at 80 % (decided 2026-11-19). A skill does not go up after a few lucky successes. At 80 %, 5 of 5 gives 0.75 and 10 of 10 gives 0.86. At 95 %, `tested` would need 16 successes in a row.
- **`deprecated`** needs n ≥ 10 and the upper Wilson bound ≤ 0.2: the skill is reliably bad. 1 of 10 is not yet deprecated (upper bound 0.28).
- **A tested skill goes down** when one of its last 10 grades is a failure. It goes to `wip`, or lower when its record is weaker. The last 10 are by the time of the grade.
- **Only robot grades count** (decided 2026-11-19). The fake robot tests the plumbing, not the skill. Each staged step says `robot: true` or `false`. The executor parameter `on_robot` is false unless the launch file sets it: true, except with `fake:=true`.
- **Success is not the skill's own report.** Example: `PressController` reports success when the finger did not touch the button. A use counts only through the agent's grade of its step ("Credit assignment"). When the contract has a `check` (another skill, for example a VLM question that the button is lit), a `worked` grade must agree with it. The agent's grade is enough, also for `tested` (decided 2026-11-19). A person can grade a step too, and the person's grade wins.
- **Where the counts live:** the committed grades, in a log next to the knowledge store. The counts for a skill version are made from that log, not kept apart.
- **The earned maturity is computed, not written** (decided 2026-11-19). `orchestrator/graduation.py` makes it from the log when the catalog or the Skills tab is read. No file changes.
- **What is shown** is the higher of the contract's `maturity` (a person's word) and the earned one. `deprecated` always wins. The catalog says `maturity: wip (graded on the robot, this version: 6 worked, 0 failed)`, and the Skills tab shows `wip 6/6`.
- The names stay `untested`, `wip` and `tested`. `deprecated` is an earned value only.
- **Not yet:** maturity changes nothing at run time: not the planner order, and not the rule that only a `wip` or `tested` tree runs a blind segment. Built: the shown maturity of a composition is at most that of its lowest part.

### Credit assignment: the agent grades each step

A step's raw end is not evidence about its skill. The knowledge store has the same rule for claims (`docs/knowledge.md`): a success can mean no more than "the message was published", and a failure can have another cause. So the agent assigns the credit, and skills use the same grades as claims do.

- **After each step,** the agent looks at the result. It can improve the skill at once: it writes the code, so a rewrite is a new version.
- **After the mission (retrospection),** the agent reads the whole run and grades the skill of each step. It can grade until the next mission opens. The grades are staged with the claim grades and committed together (`agent_cli commit`).
- **The grades:**

| Grade | Means | Counts as |
|---|---|---|
| `worked` | the skill did what the subgoal needed, and the agent saw it | a success |
| `failed` | the skill did not do its job, and the skill is at fault | a failure |
| `misused` | the agent chose the wrong skill or gave wrong inputs | nothing |
| `blocked` | a device or the place was the cause (for example the Orin was down) | nothing |

- Each grade needs a reason. A step with no grade counts nothing.
- **A grade belongs to one version** of the skill: the digest at that step, as for a claim grade. The record of an old version does not graduate a new one.
- **Built** (`orchestrator/step_grades.py`):
  - Each step that ran (success or failure) is staged in `knowledge/staged_steps/<run_id>.json`. The entry has the skill's version from when the step started.
  - The version is `skill_review.grade_version`: a hash of the contract and the code, without the agent's marks. So Keep does not start a new version.
  - `agent_cli grade-step N GRADE --reason R` grades a step. `agent_cli commit`, or the next mission's open, writes the graded steps to `knowledge/step_grades.jsonl`.
  - Not yet: grades of parts. A tree's result names the failed part (`failed_part`); a code skill's result does not yet.
  - Not yet: a `worked` grade is not checked against the contract's `check`.
- **A composition and its parts:** the agent grades the composition. When it failed, its result names the part that failed and why ("Compositions"). The agent grades that part too, so the credit goes to the part at fault. The other parts get no grade from that run.
- **Why grades, not raw counts:** a skill that the agent used badly, or that ran while a device was down, must not lose trust. A skill that reported success but did nothing must not gain it.

### Compositions: behavior trees that graduate

A behavior tree is a composition that the agent makes from steps that worked, and it graduates like any other skill. A code skill is a composition too, and is built now. But a composition that only calls skills is a tree ("Where the agent decides").

- **One task is one run.** For a new task, the agent works step by step: it calls a skill, looks at the result, and selects the next one. It can call many skills in one task. The executor runs each call, so the contracts, the validator, the recorder and the `rt/*` ban still apply. The recorder makes one bundle (one bag) per task, not per tree.
- **The agent makes the tree.** When the same steps work in more than one run for one kind of task, the agent writes them as a BT (with the checks and fallbacks it saw were necessary) and a contract for it. It reads the runs one at a time with `retrospect list` and `show` (`docs/recorder.md`), and compares them itself. It writes the tree with `agent_cli write-skill FILE --tree T.xml`, as any skill ("A tree skill"). Retrospection is general: it makes all skills and knowledge better, so no tool is only for trees (decided 2026-11-20). The tree is then a compound skill, at `untested`.
- **It graduates with the same rules** as the table above: the same counts, the same `check`, the same human gate before the first robot use.
- **A composition is never more trusted than its least trusted part.** Its maturity is at most the lowest maturity of the skills it calls.
- **Its result names the part that failed,** with that part's result. Then the agent can grade that part, and it can go down a level: run the parts as separate steps and decide after each one.
- **The task agent calls a graduated tree as one skill**, for example `call_elevator`. It uses the biggest trusted skill that covers a subgoal.
- **Blind segments need a trusted tree.** In the cabin there is no cloud, so the agent cannot work step by step there. Only a tree at `wip` or `tested` can run a blind segment. None of its parts may need the cloud. The agent knows about such a place from a claim in the knowledge store or from the mission sentence. It plans the segment as one step before it goes in. If no such tree exists yet, it warns on `/speak` and tries (decided 2026-11-19): its best untested tree as one step, or step by step. A drop is then handled as in docs/task-mode.md "Handle, for now". The grades of that run are how the tree earns trust; a rule to give up would keep it untested.

#### A tree skill (design, 2026-11-19)

- **Files:** a folder with `contract.yaml` (`interface: {transport: tree}`, its inputs and outputs) and `tree.xml`, a BehaviorTree.CPP v4 file. The tree can use only skills that the library knows, and the control nodes the engine has (Sequence, Fallback, RetryUntilSuccessful, and others).
- **The agent writes it** with `agent_cli write-skill FILE --tree T.xml`. The tree validator checks it at once: known skills, their ports, the deny list, no loop through itself.
- **Ports:** the tree reads its inputs as blackboard keys with the port names, for example `{goal}`. It also sees the places of the task. When it ends, each output port takes the blackboard key with its name. Nothing else goes back to the step's blackboard.
- **It runs as one step.** The step's tick also ticks the tree, because nothing inside it waits for a person. Each part is a leaf, so the contracts, the timeouts and the recorder see each part.
- **The gate (decided 2026-11-19):** a tree is just a skill. The whole tree gets the gate once, as a step: the operator runs the whole tree or not. Its parts do not ask. A code skill is the same: its calls do not ask (`gate.yaml` is gone).
- **The result of a failed tree** says the part that failed, its path in the tree and its message. The outputs carry it as `failed_part`. The agent grades the tree, and it can grade that part.
- **Maturity:** the shown maturity of a tree, or of a code skill, is at most the lowest shown maturity of its parts. A deprecated part counts as untested there. The agent may use untested parts (decided 2026-11-19): then the tree earns nothing until its parts do.
- **The cloud:** a skill needs the cloud when it, a condition that it names, or a part at any depth needs it (`Registry.cloud_part`, read from the bodies when asked, 2026-11-19). A call that does not name its skill counts too. The catalog line names that part, and a blind task refuses the skill.
- **Limits:** a tree inside a tree, or inside a code skill, counts toward the same depth limit as code skills (3). A tree cannot run itself, also through another tree.
- **Built** (`orchestrator/tree_skill.py`, 2026-11-19): all of the above. The Skills tab shows the tree and a `tree` pill. The catalog says when a part holds a composition down.
- **The cabin:** the operator approves the tree once, before the robot goes in. Nothing inside it asks, so it can run with no network.

Experiment: can the agent find the compositions alone? For the test, the agent must not see trees or procedures that people wrote. Keep them outside the agent's view (the session's permissions, the code sandbox), to compare with what the agent makes.

Built: task mode and one run per task (`docs/task-mode.md`), code skills, the grades of steps (`step_grades.py`, `agent_cli grade-step`), graduation (`graduation.py`), tree skills (`tree_skill.py`) with the failed part in the result and the maturity limit of a composition. Not built yet: the failed part in a code skill's result.

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
