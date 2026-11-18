# Reasoning areas, knowledge graduation and map annotation (design, not built)

Status: 2026-11-05. Built (except `scratch/<run_id>/` for the agent): `orchestrator/knowledge.py` (claims, tiers, rules, log), the store's guide in the open task, `--applies` evidence, the `knowledge` CLI. Not built yet: reasoning on a contradiction, `{fact:...}` in trees, claims from `retrospect`. Places come from `map_info`'s latched `~/places` (2026-11-09), with kind, floor and note; floor links wait for a decision on the nav side. Depends on `docs/recorder.md` (evidence) and `map_info` (places).

Built rules: observation → belief at 2 runs and 2 sources; a belief is ready for a fact when the Wilson lower bound (z = 1.645) is at least 0.8, that is 11 confirmations with no contradiction; the operator approves it with `knowledge approve`, at a terminal only.

## Goal

- The task agent has files to reason in, which last across steps and runs.
- What the robot sees becomes knowledge in steps: **observation → belief → fact**. A step up needs independent confirmations; a contradiction moves it down.
- A claim can be about anything: a place, a skill, a device, an object, or nothing. Knowledge about a place (a press offset, an arrival overshoot, "this button does not light") is linked to the place in `map_info`.
- A contradiction of a belief or a fact that a run uses starts reasoning ("Contradictions").
- Skills graduate the same way (`docs/skill-packages.md`, "Graduation"), from the same counts.

## Split of work

| Owner | Holds |
|---|---|
| `map_info` (navigation stack) | Geometry: places, regions, floors, their poses, revisions |
| Knowledge store (orchestrator) | Claims: what is true, with evidence, counts, tier, and the place it is about |
| Recorder (`docs/recorder.md`) | Evidence: runs, records, frames, decisions |

The graduation rules are in one place, the orchestrator. `map_info` does not judge claims.

## Reasoning areas

On the NUC, the store is in `$EIGEN_VECTOR_WS/agent/knowledge/` (`knowledge_dir`), beside the agent's contracts and code; the scratch folders are under `~/orch_knowledge/scratch/` (`agent_scratch_dir`):

| Area | Lifetime | Who writes | Holds |
|---|---|---|---|
| `scratch/<run_id>/` | one run | the task agent | notes while it works: plan, expectations, what it checked. Saved into the run folder at the end |
| `claims.yaml` | until removed | the store only (through its API) | every observation, belief and fact, with evidence |
| (prompt text) | each prompt | made from `claims.yaml` in memory (`prompt_section`) | the facts and beliefs, as text, for the prompts |

- The run's scratch folder is `scratch/<run_id>/` (built): the open task names it, and the recorder moves it into the run's bundle at the end of the run.
- The task agent gets the store's guide when a task opens: a session reads the claims it needs with the CLI, and the codex loop gets the facts and beliefs in each prompt.

## Claims

```yaml
- id: C-0042
  text: The freight elevator call panel does not light when pressed.
  about: {place: freight_elevator_call_panel}   # open tags: place, skill, device, object, ...; or {} (general)
  key: lights_when_pressed                       # optional: a value claim, compared by key
  value: false
  tier: fact                                     # observation | belief | fact | retired
  valid_for_s: null                              # null: no end; 600: a person in the hall
  confirmations: [{run: 20261104-1902-a1/runs/3, record: 17, by: agent:task}, ...]
  contradictions: []
  first_seen: 2026-11-04T19:02:11Z
  last_seen:  2026-11-05T10:15:40Z
  approved_by: null                              # a person, for the step to fact
```

### Subject (`about`)

- `about` is an open set of tags. A place is one kind of tag, not a need. Examples:
  - `{place: freight_elevator_call_panel}`: "The call panel does not light."
  - `{device: thor}`: "The VLM on Thor is slow after 30 min."
  - `{object: freight_elevator}`: "The door stays open for 8 s."
  - `{skill: press_button}`: "A press needs 2 tries on a dirty panel."
  - `{}`: "The cabin has no network."
- A value claim matches an existing claim by the same tags and `key`.
- Only a `place` tag links to `map_info`, and its name must be a `map_info` name.

### Where claims come from

- The retrospection operator (`retrospect`): from a run, for example the arrival error at each place, or the offset that made a press light.
- A person: by hand, at a terminal (`knowledge note --by operator`).
- The task agent, as a session (built 2026-11-17): it writes with `knowledge note|confirm|contradict --by agent:task`, for example after a failure what to do instead ("When <what failed>, <what to do instead>"). The CLI uses the executor's code clock: the executor saves where it reads the code in `<knowledge_dir>/code_clock.yaml`, so a claim about a skill keeps the code under it. `--by operator` works only at a terminal.
- How the task agent learns the store (built 2026-11-17): the open task carries `TaskDesk.known()`: `KnowledgeStore.guide()` (the rules, made from the store's constants, and an index of subjects) and how to write (`writing_text`). A session reads the claims it needs with `knowledge list|show`; the codex loop has no tools, so its prompt carries the claims (`known(claims=True)`), and it cannot write.
- A known limit: the code clock follows the `skill` tag only. A value measured through a skill but tagged only with a place (an arrival offset) depends on that skill's code too, and is not reset. Give such a claim the skill tag as well.
- A step that follows a claim (`applies: <id>` on a run): the step's end confirms it (success) or contradicts it (failure), by `outcome:<skill>`. This is weak evidence, because a success can mean only that the message was published: it counts in reliability, not as a source for graduation.

### Graduation

| Step | Rule (first guess, for review) |
|---|---|
| observation → belief | seen in 2 runs or more, from 2 sources or more (a skill check, a frame judged by a VLM, a person) |
| belief → fact | Wilson lower bound of confirmations / (confirmations + contradictions) ≥ 0.8, and the operator approves the text |
| fact → belief → observation | one contradiction moves a claim down one tier; it is logged and shown in the next prompt |
| any → observation | a claim with a `skill` tag: the code under the skill moved (`skill_stamps.SkillClock`, the same rules as a skill stamp). Its sightings are kept but do not count, and it earns its tier again on the new code (built 2026-11-17) |
| any → retired | `valid_for_s` passed, or a person retires it |

- A confirmation must come from evidence (a check skill, a judged frame, a measured value, a person), not from the agent's own text.
- Value claims (with `key`) keep the values, so a mean and a spread can be computed: for example the arrival overshoot at a place from all runs.
- No claim becomes a fact without the operator's approval of the exact text.

### Approval by the agent (later)

The operator approves for now. To let the agent approve later, record every operator decision (claim, evidence shown, approve or refuse, the edit made) in the recorder. Then:

1. **Shadow:** the agent proposes approve or refuse for each claim; the operator still decides. Count how often they agree.
2. **Narrow:** when the agreement is high for one kind of claim (for example value claims with ≥ 10 confirmations), the agent approves that kind alone; the operator can undo.
3. Facts used by a robot skill that moves (press offsets, stand poses) stay with the operator longest.

## Contradictions

A contradiction of a belief or a fact starts reasoning when the current run uses that claim. How critical it is depends on the use, not on the claim: a wrong press offset matters when the arm moves, not when the robot walks.

### What "used" means

- The tree reads it: a contract input that reads a claim (`{fact:...}`) has a mark, for example `press_offset: {fact: freight_elevator_cabin_panel.press_offset, critical: true}`.
- The task agent read it in this run (not critical by default).
- The executor keeps the list of claims that the current run uses, with the highest mark of each.

### Flow

1. The store records the contradiction. A fact moves down to a belief (as now). The store sends an event with the claim, the new sighting and its evidence.
2. The executor checks whether the current run uses the claim, and how critical the use is.
3. When the run uses it, the executor asks the task agent to review it (`knowledge_review`). The agent reads the history of the claim and the evidence in the recorder, and selects one:
   - **continue**: the new sighting is noise; keep the value;
   - **use with care**: keep the value and add a check after the step;
   - **ask the operator**;
   - **replan**;
   - **stop safely**.
4. When the run does not use it, the store only logs it. It goes in the next prompt and in the operator's review list.
5. Blind mode (no cloud): a fixed rule. A critical use goes to the tree's fallback; a use that is not critical gives a warning and the tree continues.
6. The recorder keeps each decision with its evidence. Later these teach the agent which contradictions matter.

## Map annotation

Knowledge about a place is a claim with `about: {place: <name>}`. `map_info` gives the names and poses; the store gives what is known there.

Changes to `map_info` (the user's package, built for the orchestrator):

1. `SaveAnnotation`: an optional pose (`geometry_msgs/PoseStamped`). Empty: the robot's averaged pose, as now. So the agent or a person can annotate a place where the robot does not stand.
2. A `revision` per annotation and a `base_revision` in `SaveAnnotation` and `DeleteAnnotation` (as HomeBody's `update_region`): a write on an old revision is refused, so two writers do not overwrite each other.
3. `GetPlace` returns the annotation's `revision`, `source` (saved pose, given pose) and the SLAM inliers at save time.
4. Markers: a claim count per place in the RViz label (optional, read from the store).

Examples of place claims:

- the call panel does not light;
- the press offset that lights the floor 1 button inside the cabin;
- the arrival overshoot at the call panel and inside the cabin.

## Use in the tree

- A skill input can read a fact value: `offset="{fact:freight_elevator_cabin_panel.press_offset}"`. The validator refuses a key that has no fact (as it refuses blackboard keys that are never set).
- The trees then do not carry offsets per panel; they read them from the facts.

## For the UI (later)

The UI reads files, so it needs no ROS and works on the laptop after an rsync:

- `agent/knowledge/claims.yaml`: every claim with its tier, subject, value and sightings (each with its run).
- `agent/knowledge/log.jsonl`: every change, who made it, and the operator's approvals and refusals.
- `~/orch_runs/<session>/runs/<n>-<task>/`: the missions (task, steps, records, plans, outcome), linked from the claims by `run_id`.

## Open questions

- One `claims.yaml`, or one file per place?
