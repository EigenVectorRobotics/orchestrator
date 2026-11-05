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

Unless an entry says otherwise, "the field test" is the walk-and-press test of 2026-11-04 on the Stanley G1 at the freight elevator: the outside call panel and the inside floor panel, `manipulation_stack` on `test/pelvis-correction` with edits not yet committed, and the navigation stack with edits not yet committed.

### E-001 After a walk, the robot stops a few centimetres off its place
- Status: confirmed (field test)
- Conditions: walks to the outside call-panel place; `path_follower` `slow_down_distance_threshold` set live to 0.7 (config 0.4; a relaunch resets it)
- Cause: the robot stops once within the arrival thresholds (`goal_reached_threshold` 0.15 m, `goal_yaw_threshold` 0.15 rad; `path_follower` `position_tolerance` 0.08 m), not on the goal
- What was seen: the walk overshoots the call-panel place by 3 to 6 cm. An arrival more than about 8 cm from the place usually fails the reach check, so correct it at once rather than trying the press. Earlier the team gave about 0.1 m as the final accuracy, and in simulation SONIC stopped about 13 cm off
- What helped: plan a stance that stays reachable across a few centimetres of error; correct straight away after a poor arrival (E-002, E-013)
- Source: field test notes, 2026-11-04; the team, 2026-11-04

### E-002 A goal close to where the robot stands ends without a step; resending the same goal can overshoot
- Status: confirmed (field test); mechanism read from the code
- Conditions: the deployed navigation stack
- Cause: `navigate_to` succeeds when the local planner sees the robot within `goal_reached_threshold` (0.15 m) and `goal_yaw_threshold` (0.15 rad) of the goal. A new goal nearer than that is reached at once, so the robot does not move. The field notes describe this as route_planner ignoring goals closer than its tolerance; the route planner itself refuses goals only when it has no terrain graph or no free ground near the goal. Resending the same goal from where the robot stands can make it overshoot
- What helped: for a correction smaller than about 0.15 m, a local move (E-013), or walking out and in again; after three failed corrections, walk out and in again
- Source: field test notes, 2026-11-04; `local_planner/src/local_planner.cpp`, `route_planner/route_planner/route_planner_node.py`

### E-003 The seed locks onto a button other than the one asked for
- Status: confirmed (field test, once); the mechanism is also documented in track_anything
- Conditions: inside floor panel, Cosmos grounding on the Thor; floor 1 asked while it sat below the bottom edge of the frame
- Cause: the grounding model returns a plausible box even when the requested object is not in the image, and a panel of similar buttons gives it one: with floor 1 out of frame, Cosmos grounded floor 2 and the arm pressed floor 2. track_anything documents the same: "A local VLM answers a plausible box for an object that is not there, so one query alone is a poor presence test" (`track_3d_parameters.yaml`, `vlm_min_agree`)
- Signs: the requested button is not in the camera frame, or only at its edge; the tracked target sits where another button is (for floor buttons, a neighbour in the same column); at about 0.38 m from the inside panel, floor 1 can fall out of frame
- What helped: look at a frame before pressing and confirm the requested button is in it. If it is not, move so that it is (usually back, or toward it) before seeding again. Walking closer does not fix a wrong target
- Source: field test notes, 2026-11-04; track_anything `main`

### E-004 NO_TRACK is often random
- Status: confirmed (field test)
- Conditions: the reach check before a press, seeding through Cosmos
- Cause: [TODO: not known]; the seed alone takes 1.7 to 9 s
- What helped: if the button is in frame, run the seed and the check again, up to twice, before moving the robot
- Source: field test notes, 2026-11-04

### E-005 NO_TRACK with a large, close button: the robot is too close
- Status: confirmed (field test)
- Conditions: any panel
- Cause: the D435 gives no depth closer than about 0.28 m, so the button has no 3D points and no pose
- What helped: step back. A button that fills much of the frame and gives no track points to this, not to a perception failure
- Source: field test notes, 2026-11-04

### E-006 The reach that works depends on the panel
- Status: confirmed (field test)
- Conditions: target position ahead of the pelvis, as the reach check prints it
- What was seen: outside call panel, 0.33 to 0.48 m ahead worked, and the operator called 0.338 and 0.354 m good. Inside floor panel, about 0.38 to 0.46 m; 0.52 to 0.53 m was too far, and at 0.38 m floor 1 can fall out of frame (E-003). On both panels, stay within 0.15 m sideways
- What helped: aim a reposition at the middle of the panel's range, not its edge
- Source: field test notes, 2026-11-04

### E-007 Which arm: by side outside, the right arm inside
- Status: confirmed (field test)
- Conditions: the two panels as in E-006
- What was seen: outside, pick the arm on the button's side (left when the button is at y > 0 in the pelvis frame). Inside, the left arm failed IK (`Planning failed: empty trajectory`) for floor buttons at y +0.05 to +0.07 m and z 0.22 to 0.24 m above the pelvis; the right arm reached them
- What helped: when planning returns an empty trajectory, try the other arm before moving the robot
- Source: field test notes, 2026-11-04

### E-008 After a walk, the body EKF's yaw drifts and drags foot_world
- Status: confirmed (field test)
- Conditions: right after a walk; the EKF assumes a still robot
- Cause: the EKF yaw drifts by up to 0.35 deg/s after a walk. `foot_world` turns with it, and the tracked target, which is filtered in `foot_world`, moves by centimetres during a press
- Signs: the target wanders in `foot_world` while the camera-frame track stays steady (E-009)
- What helped: reset the body EKF once the stance is good, before the press. A yaw rate above about 0.05 deg/s after a reset means the reset did not take
- Source: field test notes, 2026-11-04

### E-009 Judge a press in order: did the robot step, was the track steady, then where did the fingertip end
- Status: confirmed (field test)
- What was seen: a leg joint swinging more than about 5 deg means the robot stepped, and the press is invalid (once, 26.6 deg on the left knee); retry it with no correction. Distrust a press when the target wanders more than about 20 mm in `foot_world`, or the torso yaw moves more than about 3 deg; a steady camera-frame track with a moving `foot_world` target points at the EKF (E-008), not the tracker. Only then read the fingertip residual: fingertip minus target in the target frame (x along the normal, y sideways, z up)
- What helped: take a correction only from a press that passed the first two checks (E-012)
- Source: field test notes, 2026-11-04

### E-010 Most misses were not the arm controller
- Status: confirmed (field test)
- What was seen: a free-air replay of the same arm commands tracked within 0.2 deg. The large errors came from steps, a drifting EKF, the hand meeting the panel, or poor positioning
- What helped: look for those causes before suspecting tracking control
- Source: field test notes, 2026-11-04

### E-011 With the aim right, the fingertip still stops a few millimetres short: the waist yaw gives way
- Status: confirmed (field test); the remedy is open
- Conditions: inside floor panel, right arm, floor buttons, which light a ring when pressed
- Cause: the fingertip stalls 4 to 7 mm short of the planned depth as the waist yaw gives way by about 0.5 deg when the finger meets the button (VisualServo waist kp 200, Ki 60). The planned depth includes the depth offset, so the offset was applied. Self-collision is ruled out on the URDF's hulls (closest pair 19 to 23 mm); parts missing from the URDF (cables, gripper mounts) could still touch
- Signs: the finger sits on the button face and the button does not light; the reached depth falls short of the planned one, with a jump in waist yaw error at that moment
- What helped: not yet solved. Options under consideration: a larger depth offset (about +15 mm), or a stiffer or locked waist yaw during the press. Retrying the same press is unlikely to help
- Source: field test notes, 2026-11-04

### E-012 The fingertip error repeats for a given panel and arm, so a correction from clean presses helps
- Status: confirmed (field test, few presses)
- Conditions: one panel and one arm at a time; the values below are from the inside panel with the right arm on one day and are a guide to the size and direction, not settings
- What was seen: clean presses with no offset landed close to each other, a few millimetres short, sideways and low (on the order of 5 to 8 mm); applying the negative of their average moved the finger onto the button face. A correction taken from a press where the robot stepped or the track drifted sent the hand further off. On the outside panel the error differed from the inside one. The final-leg stepping lost part of a small depth offset (about 3 of 5 mm at its last step)
- What helped: correct with `arm_safety_distance_{x,y,z}` set to the negative residual of clean presses only (E-009); keep corrections per panel and per arm; prefer the median of several clean presses to one
- Source: field test notes, 2026-11-04

### E-013 Local moves are open loop and unreliable for small corrections
- Status: confirmed (field test)
- Conditions: a `TwistStamped` at 0.2 m/s on `/local_movement_cmd_vel` for distance / speed seconds, in HighLevel mode; `cmd_vel_mux` gives it priority over navigation
- What was seen: forward moves covered about half the command (9 cm moved 2 cm, 21 cm moved 10 cm, 20 cm about 8 cm); an 8.6 cm back move went about 21 cm
- What helped: when 5 cm or more too far, command forward about twice the error. When too close, or slightly too far, step back about 20 cm, then forward, sizing the forward leg for the half coverage. After three failed corrections, walk out and in again (E-002)
- Source: field test notes, 2026-11-04
