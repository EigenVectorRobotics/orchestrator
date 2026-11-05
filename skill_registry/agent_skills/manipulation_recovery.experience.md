# Manipulation recovery: experience

What the team has seen on the robot and in tests about press failures, their causes, and what helped. `manipulation_recovery.md` says how to use this file; read that first.

Facts that hold by construction (code, configs, the URDF) belong in `manipulation_recovery.md`. This file holds what was observed: it can depend on conditions, and an entry can be wrong.

## How an entry is written

```
### E-<number> <the symptom, as it shows in the evidence>
- Status: confirmed (n times) | reported | suspected
- Conditions: where and with what it was seen (place, stance, software versions)
- Cause: what caused it, and how that was checked
- What helped: the decision or fix, and the result
- Evidence: trace, bag, issue or file
- Source: who saw it and when; who confirmed it
```

- **confirmed**: seen on the robot and the cause checked, at least once; n counts the times.
- **reported**: stated by a teammate or written in a repository, not yet checked in a recorded run.
- **suspected**: a cause someone proposed from one run; not checked.

**Who writes.** A person, or an agent that drafts an entry and has a person approve the exact text before it goes in. An agent never adds or edits entries on its own. An entry that turns out to hold by construction moves to `manipulation_recovery.md` as a fact and leaves this file.

## Entries

### E-001 The robot stops up to about 0.1 m from its navigation goal
- Status: reported
- Conditions: G1, deployed navigation (`navigation_stack` main @ `6242b3e`), holonomic final approach
- Cause: the final tolerance of the approach. The deployed `local_planner/config/unitree_g1.yaml` still lists `goal_reached_threshold` 0.15 m and `goal_yaw_threshold` 0.15 rad
- What helped: treat the stance after a walk as the goal ±0.1 m; a correction smaller than about 0.1 m is within that spread. Earlier, in simulation, the walking controller (SONIC) stopped about 13 cm off target (orchestrator commit `19d991b`); the 0.1 m figure supersedes it on the robot
- Evidence: none recorded yet [TODO: a run that measures the final offset]
- Source: the team, 2026-11-04 [TODO: who measured it]

### E-002 A short move is carried out; the local planner does not refuse a goal near the robot
- Status: reported
- Conditions: as E-001
- Cause: [TODO]
- What helped: a small reposition is executed, but its effect is limited by E-001
- Evidence: none recorded yet
- Source: the team, 2026-11-04

### E-003 The seed locks onto an object that is not the button
- Status: reported
- Conditions: track_anything `main` with the local VLM
- Cause: "A local VLM answers a plausible box for an object that is not there, so one query alone is a poor presence test" (track_anything `track_anything/track_3d_parameters.yaml`, description of `vlm_min_agree`)
- What helped: [TODO: no recorded case]. Expected sign: the target position is implausible for the button asked for (far from the panel, at an odd height); walking closer does not fix it, so the decision is `ask_help`
- Evidence: none recorded yet
- Source: the perception code's own documentation, read 2026-11-04
