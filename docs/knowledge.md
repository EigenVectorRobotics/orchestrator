# Reasoning areas, knowledge graduation and map annotation (design, not built)

Status: draft for review, 2026-11-05. Depends on `docs/recorder.md` (evidence) and `map_info` (places).

## Goal

- The planner and the agent have files to reason in, which last across calls and runs.
- What the robot sees becomes knowledge in steps: **observation → belief → fact**. A step up needs independent confirmations; a contradiction moves it down.
- Knowledge about a place (a press offset, an arrival overshoot, "this button does not light") is attached to the place in `map_info`.
- Skills graduate the same way (`docs/skill-packages.md`, "Graduation"), from the same counts.

## Split of work

| Owner | Holds |
|---|---|
| `map_info` (navigation stack) | Geometry: places, regions, floors, their poses, revisions |
| Knowledge store (orchestrator) | Claims: what is true, with evidence, counts, tier, and the place it is about |
| Recorder (`docs/recorder.md`) | Evidence: runs, records, frames, decisions |

The graduation rules are in one place, the orchestrator. `map_info` does not judge claims.

## Reasoning areas

On the NUC, under `~/orch_knowledge/` (a parameter):

| Area | Lifetime | Who writes | Holds |
|---|---|---|---|
| `scratch/<run_id>/` | one run | the planner, the agent | notes while it works: plan, expectations, what it checked. Saved into the run folder at the end |
| `claims.yaml` | until removed | the store only (through its API) | every observation, belief and fact, with evidence |
| `facts.md` | until a fact goes down | made from `claims.yaml` | the facts, as text, for the prompts |

- The agent's sandbox `scratch_dir` becomes `scratch/<run_id>/`, so its files are kept with the run.
- The planner and the agent read `facts.md` (and the beliefs, marked as beliefs) in each prompt.

## Claims

```yaml
- id: C-0042
  text: The freight elevator call panel does not light when pressed.
  about: {place: freight_elevator_call_panel}   # a map_info name; or {skill: press_button}; or none
  key: lights_when_pressed                       # optional: a value claim, compared by key
  value: false
  tier: fact                                     # observation | belief | fact | retired
  valid_for_s: null                              # null: no end; 600: a person in the hall
  confirmations: [{run: 20261104-1902-a1/runs/3, record: 17, by: press_judge}, ...]
  contradictions: []
  first_seen: 2026-11-04T19:02:11Z
  last_seen:  2026-11-05T10:15:40Z
  approved_by: null                              # a person, for the step to fact
```

### Where claims come from

- The agent: a new tool `note(text, about, key, value, evidence)` adds an observation. It cannot set the tier.
- The retrospection operator (`retrospect`): from a run, for example the arrival error at each place, or the offset that made a press light.
- A person: by hand, for example from the experience log (`manipulation_recovery.experience.md`).

### Graduation

| Step | Rule (first guess, for review) |
|---|---|
| observation → belief | seen in 2 runs or more, from 2 sources or more (a skill check, a frame judged by `press_judge`, a person) |
| belief → fact | Wilson lower bound of confirmations / (confirmations + contradictions) ≥ 0.8, and the operator approves the text |
| fact → belief | one contradiction from a check or a person; it is logged and shown in the next prompt |
| any → retired | `valid_for_s` passed, or a person retires it |

- A confirmation must come from evidence (a check skill, a judged frame, a measured value, a person), not from the planner's or agent's own text.
- Value claims (with `key`) keep the values, so a mean and a spread can be computed: for example the arrival overshoot at a place from all runs.
- The rule of the experience log stays: no claim becomes a fact without the operator's approval of the exact text.

### Approval by the agent (later)

The operator approves for now. To let the agent approve later, record every operator decision (claim, evidence shown, approve or refuse, the edit made) in the recorder. Then:

1. **Shadow:** the agent proposes approve or refuse for each claim; the operator still decides. Count how often they agree.
2. **Narrow:** when the agreement is high for one kind of claim (for example value claims with ≥ 10 confirmations), the agent approves that kind alone; the operator can undo.
3. Facts used by a robot skill that moves (press offsets, stand poses) stay with the operator longest.

## Map annotation

Knowledge about a place is a claim with `about: {place: <name>}`. `map_info` gives the names and poses; the store gives what is known there.

Changes to `map_info` (the user's package, built for the orchestrator):

1. `SaveAnnotation`: an optional pose (`geometry_msgs/PoseStamped`). Empty: the robot's averaged pose, as now. So the agent or a person can annotate a place where the robot does not stand.
2. A `revision` per annotation and a `base_revision` in `SaveAnnotation` and `DeleteAnnotation` (as HomeBody's `update_region`): a write on an old revision is refused, so two writers do not overwrite each other.
3. `GetPlace` returns the annotation's `revision`, `source` (saved pose, given pose) and the SLAM inliers at save time.
4. Markers: a claim count per place in the RViz label (optional, read from the store).

Examples of place claims from the 2026-11-04 trial (to be checked against the experience log):

- the call panel does not light (E-019);
- the press offset that lit the floor 1 button inside the cabin (E-011, E-012);
- the arrival overshoot at the call panel and inside the cabin (E-001, E-015).

## Use in the tree

- A skill input can read a fact value: `offset="{fact:freight_elevator_cabin_panel.press_offset}"`. The validator refuses a key that has no fact (as it refuses blackboard keys that are never set).
- The trees then do not carry offsets per panel; they read them from the facts.

## Open questions

- One `claims.yaml`, or one file per place?
- Should a contradiction stop a tree that uses that fact, or only warn?
