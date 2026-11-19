# Skill structure and a shared skill node (design, not built)

Status: draft for review, 2026-11-05. Related: `docs/skill-packages.md` (the package, live skills, graduation), `docs/knowledge.md` (facts), `docs/recorder.md` (evidence).

## Problem

- There is no structure for a new skill yet. A skill's documents could put procedure, tool docs, file locations, and facts about the arm, perception and navigation in one long prompt.
- Hand-written contracts use few of their fields: `pre`, `effects` and `fallback` once each, `check` twice. No contract declares failure codes.
- Each skill is to be a node, and these nodes need a shared structure that is not known yet.

## What other work found

- **Documentation length.** Hsieh et al. 2023 (https://arxiv.org/abs/2308.00675): documentation alone is as good as or better than demonstrations for tool use; results got better with document length up to about 600 words, then worse.
- **Progressive disclosure.** The SKILL.md format (Anthropic, now an open standard; survey: https://arxiv.org/abs/2602.12430): level 1 is a short name and description, always in context; level 2 is the procedure, loaded when the skill is used; level 3 is reference files, loaded only when the procedure asks for them.
- **Conditions.** SkiROS2 (https://arxiv.org/abs/2306.17030): each skill has pre-, hold- and post-conditions; hold-conditions are checked at every tick while it runs; skills are primitive or compound, over a shared world model.
- **Evaluation.** ACES (https://arxiv.org/abs/2608.20614): evaluate a skill as an executable artifact, not only as a document.
- **Declared failures.** HomeBody: each skill declares `FAILURES`, and each attempt ends in one result with one declared code.

## Proposed structure of a skill

```
skill_registry/skills/<skill>/
  contract.yaml        level 1 and the machine part
  procedure.md         level 2: how to do it and how to decide, ≤ ~600 words
  reference/*.md       level 3: tools, topics, conventions, file locations; linked from procedure.md
  adapter/             code, when the skill is a node (see below)
  tests/               a test tree and expected results (sim, fake_robot)
```

`contract.yaml` adds to the current schema:

| Field | Holds |
|---|---|
| `description` | level 1: one or two sentences, what it does and when to use it |
| `pre`, `hold`, `post` | condition skills: before the start, at every tick while it runs, after the end |
| `failures` | the declared failure codes, each with a one-line meaning and the recovery to try (`OUT_OF_REACH: reposition`) |
| `evidence` | what the result carries for the recorder and the agent (feedback over time, frames, residuals) |
| `version`, `maturity` | as in `docs/skill-packages.md` |

Facts are not written in the skill documents. They come from the knowledge store (`docs/knowledge.md`) and the session reads them, with their tier, with `knowledge list|show`. Facts about the arm, perception and navigation become claims with evidence.

## A shared skill node

What every skill node needs, whatever it does:

- **One action shape:** goal (the contract inputs); feedback (`phase`, `progress`, the evidence fields); result (`success`, one declared `failure` code, `message`, the evidence).
- **Live:** its interface is on the ROS graph, so the registry finds it (`docs/skill-packages.md`); no announcement needed.
- **Conditions:** checks `hold` at every tick and fails with the declared code when one stops holding.
- **Resources:** claims its resources (arms, base, tracker) and refuses a goal when another skill holds them.
- **Cancel:** stops in a safe state and reports `CANCELLED`.
- **Version and recorder hooks:** the version in every result; the evidence in the shape the recorder reads.

How to find the rest: survey the nodes that the stack has now (`VisualServoingArm`, `NavigateToGoal`, `MoveToPose`, the `unitree_interface` Trigger and SetProfile services, `track_3d`). Put what they share into a base class (`skill_node.SkillNode`, Python first); keep what differs as contract fields. A stack node that we do not own gets a thin adapter node instead of a change.

## Experiment: which structure works

The recorder (`docs/recorder.md`) gives the test set.

1. **Cases:** recorded failures from the run bundles, each with the decision the operator found correct. New runs add cases.
2. **Variants of a skill's documents:**
   - A: one document, as now;
   - B: split (contract, procedure ≤ 600 words, reference files the agent opens when it needs them);
   - C: B, with facts from the knowledge store instead of fact sections.
3. **Measure:** correct decisions, tokens, time to decide, and the files opened. Run each case several times per variant (the model is not deterministic).
4. **Confirm** the best variant in the sim with fault injection, then on the robot.

The same set tests the 600-word rule for our case, and later the agent's approvals (`docs/knowledge.md`).

## Open questions

- Do we move the stack's own nodes to the shared base, or keep adapters for all of them?
- Is Python enough for the base, or do the arm and nav nodes (C++) need a C++ base too?
- Who owns the test set of cases: the operator, through `retrospect`?
