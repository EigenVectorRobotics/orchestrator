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

"The press tests" are the orchestrator runs of 2026-11-08, 14:57 to 19:45, with test trees that walk and press, or press from where the robot stands (`~/orch_runs/press_down_145709/*.xml` on the NUC; sessions `20261108T185515Z` to `20261108T233309Z`). The Thor's `ground_server` gave the button boxes and the Orin's track_3d tracked them; only the first four presses (`20261108T185515Z` runs 007 to 010) have no tracked target in their bags and may have used the fixed poses of `fake_button_pose.py`. The trees had no stance check and checked each press with `press_reached` (the fingertip against its goal, not whether the button lit). The call panel and inside places then sat a few centimetres from where they are now.

"The agent trial" is the orchestrator run of the full task on the night of 2026-11-04 to 05 (23:22 to 00:16, runs run_232207 to run_001046): walk to the call panel, press down, wait for the door, walk in, press floor 1, with `manipulation_recovery` and `press_judge` answered by a Claude Code session through `agent_cli`, the same software as the field test, and `path_follower` `slow_down_distance_threshold` back at its config default 0.4 (the stack was relaunched). Its traces are `~/orch_runs/run_*/trace.txt` on the NUC and its press frames `~/.ros/orchestrator_captures/`; run names are the start times.

### E-001 After a walk, the robot stops a few centimetres off its place
- Status: confirmed (field test)
- Conditions: walks to the outside call-panel place; `path_follower` `slow_down_distance_threshold` set live to 0.7 (config 0.4; a relaunch resets it)
- Cause: the robot stops once within the arrival thresholds (`goal_reached_threshold` 0.15 m, `goal_yaw_threshold` 0.15 rad; `path_follower` `position_tolerance` 0.08 m), not on the goal
- What was seen: the walk overshoots the call-panel place by 3 to 6 cm. An arrival more than about 8 cm from the place usually fails the reach check, so correct it at once rather than trying the press. Earlier the team gave about 0.1 m as the final accuracy, and in simulation SONIC stopped about 13 cm off
- Agent trial (slow-down threshold back at 0.4), arrivals read from `/odom_base_link`, in the place's frame (along its heading, and sideways): at the call panel, from the preapproach, 1 cm short and 5 cm right (1.749, -1.398); 8 cm past, 10 cm right and 9 deg yawed (1.823, -1.471); 17 cm past and 9 cm right (1.913, -1.491). Inside, the walk from the call panel ended 24 cm past the place (3.221, -0.142); a 0.35 m walk from behind ended 3 cm past and 5 cm left (3.381, -0.296). Two reposition walks of 0.27 and 0.29 m ended 7 cm short of and 8 cm beyond their goals. Each arrival 8 cm or more past the place put the requested button out of the camera's view (E-015)
- What helped: plan a stance that stays reachable across a few centimetres of error; correct straight away after a poor arrival (E-002, E-013); in the agent trial, a reposition of 0.25 m or more (E-016). [TODO: set `slow_down_distance_threshold` 0.7 again and compare; or move the places back by the mean overshoot]
- Source: field test notes, 2026-11-04; the team, 2026-11-04; agent trial runs run_235411, run_235844, run_000131, run_001046 (`/odom_base_link` read at each arrival)

### E-002 A goal close to where the robot stands ends without a step; resending the same goal can overshoot
- Status: confirmed (field test); mechanism read from the code
- Conditions: the deployed navigation stack
- Cause: `navigate_to` succeeds when the local planner sees the robot within `goal_reached_threshold` (0.15 m) and `goal_yaw_threshold` (0.15 rad) of the goal. A new goal nearer than that is reached at once, so the robot does not move. The field notes describe this as route_planner ignoring goals closer than its tolerance; the route planner itself refuses goals only when it has no terrain graph or no free ground near the goal. Resending the same goal from where the robot stands can make it overshoot
- What helped: for a correction smaller than about 0.15 m, a local move (E-013), or walking out and in again; after three failed corrections, walk out and in again
- Agent trial: two `manipulation_recovery` repositions ended in 0.2 s without a step, one 0.148 m from the robot (inside the threshold: the agent had left out the sideways part of the distance) and one 0.18 m from it (outside the threshold; why it ended at once is not known: E-016). Repositions of 0.27 and 0.29 m walked
- Source: field test notes, 2026-11-04; `local_planner/src/local_planner.cpp`, `route_planner/route_planner/route_planner_node.py`; agent trial run_235411, run_000131

### E-003 The seed locks onto a button other than the one asked for
- Status: confirmed (field test, once); the mechanism is also documented in track_anything
- Conditions: inside floor panel, Cosmos grounding on the Thor; floor 1 asked while it sat below the bottom edge of the frame
- Cause: the grounding model returns a plausible box even when the requested object is not in the image, and a panel of similar buttons gives it one: with floor 1 out of frame, Cosmos grounded floor 2 and the arm pressed floor 2. track_anything documents the same: "A local VLM answers a plausible box for an object that is not there, so one query alone is a poor presence test" (`track_3d_parameters.yaml`, `vlm_min_agree`)
- Signs: the requested button is not in the camera frame, or only at its edge; the tracked target sits where another button is (for floor buttons, a neighbour in the same column); at about 0.38 m from the inside panel, floor 1 can fall out of frame
- What helped: look at a frame before pressing and confirm the requested button is in it. If it is not, move so that it is (usually back, or toward it) before seeding again. Walking closer does not fix a wrong target
- Agent trial: with the requested button cut by the bottom edge of the frame, Cosmos gave 0 boxes rather than a wrong one, for both `down` and `floor_1` (E-015)
- Source: field test notes, 2026-11-04; track_anything `main`; agent trial

### E-004 NO_TRACK is often random
- Status: confirmed (field test)
- Conditions: the reach check before a press, seeding through Cosmos
- Cause: [TODO: not known]; the seed alone takes 1.7 to 9 s
- What helped: if the button is in frame, run the seed and the check again, up to twice, before moving the robot
- Press tests: 10 of 69 presses ended in `press_button`'s 60 s timeout. Of the 6 timeouts in runs with one press, 4 had no or almost no tracked target in their bags (0 to 8 messages on `/track_3d/selected_target_3d`) and 2 had one (121 and 321 messages). `ground_server` answered every grounding request that day, 4 of them with 0 boxes. Why these presses timed out is not known
- Source: field test notes, 2026-11-04; press tests (sessions `20261108T202345Z`, `20261108T211441Z`, `20261108T215835Z`, `20261108T222307Z`; the Thor's `~/eigen_vector_ws_bringup/launch.log`)

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
- Agent trial: outside, a down press with the goal 0.48 m ahead in `foot_world` reached its goal. Inside, from 13 cm behind the inside place (3.529, -0.350), `press_button` reported `Controller reached target`, but the fingertip's closest point was 67 mm short and 39 mm beside the goal, near the door buttons: out of reach, and the action did not say so (E-018)
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
- Agent trial: floor 1, right arm, offset (+5.0, +7.5, +3.4) mm. Once it lit (residual -6.2 mm deep, 7.9 mm sideways); once, after walking in again, the finger sat on the face, nearly centred (residual -2.0, -5.1, +0.2 mm), and nothing lit in the press or the release frame. An `offset_retry` to (+12.0, +12.6, +3.2) mm lit it at once (residual +8.2, -4.0, -1.1 mm). So a depth offset near +12 mm helped once; the residual depth alone does not tell a press from a touch (lit at -6.2 mm, unlit at -2.0 mm): judge by the ring (E-019)
- Evidence: the field test's floor 2 presses, checked offline with `selfcoll.py` (Pinocchio and coal on `g1_29dof_with_hand_rev_1_0.urdf`, convex hulls, adjacent links skipped) and `depthcheck.py` (planned depth, reached depth, joint error): no right-arm hull overlapped anything at the measured or the commanded hold; the planner put the depth offset into the plan (+5.7 mm at step 3), and the fingertip stalled 4 to 7 mm short with the waist yaw error jumping to +0.5 deg (bags `press_debug_20261104_190703`, `_191319`, `_191723`)
- Source: field test notes, 2026-11-04; agent trial run_234904, run_001046 (frames press_1793854177.790, press_1793855488.164, press_1793855570.844)

### E-012 The fingertip error repeats for a given panel and arm, so a correction from clean presses helps
- Status: confirmed (field test, few presses)
- Conditions: one panel and one arm at a time; the values below are from the inside panel with the right arm on one day and are a guide to the size and direction, not settings
- What was seen: clean presses with no offset landed close to each other, a few millimetres short, sideways and low (on the order of 5 to 8 mm); applying the negative of their average moved the finger onto the button face. A correction taken from a press where the robot stepped or the track drifted sent the hand further off. On the outside panel the error differed from the inside one. The final-leg stepping lost part of a small depth offset (about 3 of 5 mm at its last step)
- What helped: correct with `arm_safety_distance_{x,y,z}` set to the negative residual of clean presses only (E-009); keep corrections per panel and per arm; prefer the median of several clean presses to one
- Agent trial, inside panel, right arm, floor 1: with no offset the finger hit the right rim (residual about -9.3 mm deep, 11 mm sideways; not lit); with the field test's (+5.0, +7.5, +3.4) mm it lit once and touched without lighting once; (+12.0, +12.6, +3.2) mm lit (E-011). The trees now start the inside press at (+5.0, +7.5, +3.4) mm [TODO: start at (+12, +12.6, +3.2) mm once a second press confirms it]
- Agent trial, outside panel, right arm, down button, no offset: residuals (x in, y left, z up) -5 mm deep and 8 mm sideways, signs not recorded (pressed); (-11.3, -14.8, +0.5) mm (on the right rim; judged not pressed); (-8.7, +4.0, -0.3) mm (pressed). The sideways error changed sign between presses, so no outside offset is repeatable yet; an `offset_retry` from one rim hit is still the right recovery (E-020)
- Evidence: the field test's clean presses (no step, steady track), fingertip minus tracked target in the target frame (x along the normal, y sideways, z up), in mm, with reach ahead of the pelvis: outside, up button, left arm, 0.415 m (-7.7, -12.3, -4.1) and 0.413 m (-8.7, -0.2, -5.1), neither confirmed; inside, floor 1, right arm, 0.432 m (-6.3, -7.8, -2.9) on the rim, then 0.426 m (-4.3, -6.7, -1.5) pressed with offset (+6.3, +7.8, +2.9); inside, floor 2, right arm, 0.391 m (-4.3, -8.6, -4.9; floor 1 asked but out of frame, E-003), 0.379 m (-4.6, -6.9, -3.2) on the right rim, 0.380 m (-6.5, +1.6, +1.2) on the face but not lit with offset (+5.0, +7.5, +3.4). Bags `press_debug_20261104_182519`, `_183313`, `_183646`, `_185256`, `_190703`, `_191319`, `_191723`
- Source: field test notes, 2026-11-04; agent trial runs run_234029, run_234534, run_235844, run_000131, run_001046

### E-013 Local moves are open loop and unreliable for small corrections
- Status: confirmed (field test)
- Conditions: a `TwistStamped` at 0.2 m/s on `/local_movement_cmd_vel` for distance / speed seconds, in HighLevel mode; `cmd_vel_mux` gives it priority over navigation
- What was seen: forward moves covered about half the command (9 cm moved 2 cm, 21 cm moved 10 cm, 20 cm about 8 cm); an 8.6 cm back move went about 21 cm
- What helped: when 5 cm or more too far, command forward about twice the error. When too close, or slightly too far, step back about 20 cm, then forward, sizing the forward leg for the half coverage. After three failed corrections, walk out and in again (E-002)
- Source: field test notes, 2026-11-04

### E-014 press_reached reported a miss of 19 to 23 cm sideways after a good press
- Status: confirmed (agent trial, once: run_233103; run_225429, earlier that evening, had the same signature); fixed in `press_capture.py` on 2026-11-05
- Conditions: outside call panel, right arm, `press_reached` after `press_button` reported `Controller reached target`
- Cause: the phase tracker took the arm's swing up from its rest pose for the push. On its way up the hand passes the goal's depth about 20 cm below the button; that depth was already above half the approach (-50 mm), so the tracker took it for the push, saw the hand fall back toward the pre-grasp as the retreat, and froze before the real push. Checked: in run_233103 the action feedback had `distance_to_goal` down to 9.6 mm with a steady goal, `/right_ee_pose` matched TF `right_tcp` to 1 mm, and the operator saw the button pressed; `press_reached` said -20.6 mm deep, 229.2 mm sideways. run_225429 had the same signature (-44.9 mm, 188.6 mm)
- What helped: until the pre-grasp is seen, the tracker now counts only samples within `lateral_tolerance` (30 mm) of the button axis. After the fix the numbers matched the frames (-5 mm, 8 mm on a good down press). The check itself is now `press_judge` (E-019)
- Evidence: run_233103, run_225429; `orchestrator/test/test_press_capture.py` `test_the_swing_up_beside_the_goal_is_not_taken_for_the_push`
- Source: agent trial, 2026-11-04; the operator confirmed the press

### E-015 The robot stops too close: the requested button falls below the frame and the seed finds nothing
- Status: confirmed (agent trial, three times, both panels)
- Conditions: arrivals 8 to 24 cm past the place (E-001): (1.823, -1.471) and (1.913, -1.491) at the call panel, (3.221, -0.142) inside
- Signs: `press_button` times out at 60 s with `distance_to_goal` inf in every feedback sample (no seed); `ground_server` on the Thor logs `0 box(es) for 'down'` (or `'floor_1'`) twice; the camera shows the panel large, the requested button cut by the bottom edge or below it (inside: only the panel sign and bare steel)
- Cause: the camera looks down at the panel; the closer the robot, the lower the button in the frame. Cosmos answered 0 boxes, not a wrong button (compare E-003)
- What helped: `reposition` back, to a stance at least 0.25 m from where the robot stands (E-016). From 8 cm behind the call-panel base goal the robot ended 2 cm from the base goal with the down button well in frame, and the next press worked. Look at the camera before the press: the button should sit well above the bottom edge (down button near y 540 of 720 at the call panel; floor 1 near y 450 to 600 inside)
- Evidence: run_235411, run_000131 (`agent_cli image camera` frames at each diagnosis)
- Source: agent trial

### E-016 A reposition of about 0.15 to 0.18 m ends at once, without a step
- Status: confirmed (agent trial, twice); cause suspected
- Conditions: `manipulation_recovery` reposition right after a press, robot standing still, autonomy turned on just before the walk
- What was seen: `navigate_to` succeeded in 0.2 s and the robot did not move, once with the goal 0.148 m away (inside `goal_reached_threshold` 0.15 m, so expected) and once 0.18 m away (outside it). Repositions 0.27 and 0.29 m away walked (2.8 and 2.4 s) and ended 7 and 8 cm from their goals
- Cause: for 0.148 m, the threshold (E-002). For 0.18 m, suspected: a `/goal_reached` left from the previous arrival, or the follower's tolerance; [TODO: read `/goal_reached` and `/route_planner/status` during such a walk]
- What helped: make a reposition at least 0.25 m from where the robot stands (`/odom_base_link`), counting the sideways part of the distance too. When the stance needed is closer than that, walk out and in again: a goal 0.35 m behind the place, then the place (tree `freight_elevator_floor1_walk_in_again_agentic.xml`; a literal pose with z, `"x y z yaw"`). After it the robot stood 3 cm past the inside place and 5 cm left of it, and the presses there reached floor 1
- Evidence: run_235411, run_000131, run_001046
- Source: agent trial

### E-017 The walk from the call panel into the cabin routes but finds no free path
- Status: confirmed (agent trial, once)
- Conditions: right after the down press, the door open, robot at the call panel facing the panel, the walk to the inside place
- Signs: `/route_planner/status` `arrived` with `dist_to_goal` 1.9 m (the goal within lookahead), `/free_paths` empty (width 0), `/path` a single pose, the robot not moving; `/terrain_map` held points 0.19 to 0.20 m ahead of the robot, up to 1.5 m high; `navigate_to` timed out at 120 s
- Cause: latched terrain points near the robot (the panel column it had just pressed, or stale points from the arm): one `/clear_terrain_map` freed 3154 paths at once, so they were not a real obstacle
- What helped: clear the terrain map and walk again. Every walk in the trees now does this, up to three times (`clear_terrain_map` skill); the next two walks into the cabin needed no clear
- Evidence: run_234029
- Source: agent trial

### E-018 press_button reported `Controller reached target` while the fingertip stopped 67 mm short
- Status: confirmed (agent trial, once)
- Conditions: inside panel, right arm, floor 1, the robot 13 cm behind the inside place (about 0.55 m to the button)
- What was seen: the action succeeded in 15.4 s (others took 19 to 21 s); the fingertip's closest point was 67 mm short and 39 mm beside the goal; no press frames were kept (no pre-grasp was seen); a live frame showed the hand low and right, near the door buttons. The success of the action does not prove that the arm reached the button
- What helped: judge every press from the frames and the fingertip numbers (`press_judge`); a press that ends far short is out of reach: reposition closer (E-006, E-016)
- Press tests: twice, a walk into the cabin and a floor 1 press (`floor1_walk_in`, right arm, offset (+5.0, +7.5, +3.4) mm) ended with `Controller reached target` and the fingertip's deepest point 37.5 and 48.5 mm short of its goal. There was no stance check, so the reach at the time is not known
- Evidence: run_000131; press tests `20261108T211441Z` run 013, `20261108T215835Z` run 001
- Source: agent trial

### E-019 Judging a press from the frame at the fingertip's closest point
- Status: confirmed (agent trial, eight presses judged)
- Conditions: `press_judge` after each `press_button`; the session looked at `press` (first frame after the closest point), `release` and the crops
- What was seen: floor buttons light a blue ring at once, visible in the press frame and still in the release frame 0.5 s later; a finger on the face with no ring was not a press (E-011). The call panel does not light: there the press frame often shows the hand covering the button, and the release frame, with the finger backing off, shows where it sat; a finger on the right half of the down button, 17 mm sideways by forward kinematics, was judged a rim hit. A press with no frames (no pre-grasp seen) was judged from a live frame and the numbers (E-018). The crops were sometimes too dark or off the finger to help; the full frames decided
- What helped: judge by the ring on floor buttons and by the finger's position on the call panel, and use the residual (`residual_m`) as a check, not as the answer
- Evidence: run_234029, run_234534, run_234904, run_235844, run_000131, run_001046
- Source: agent trial

### E-020 offset_retry: set the press offset from the residual and press again
- Status: confirmed (agent trial, once inside); tried once outside before a bug stopped it
- Conditions: a press that reached its goal and missed by millimetres (rim, beside, or on the face without lighting)
- What was seen: inside, floor 1 on the face unlit with offset (+5.0, +7.5, +3.4) mm and residual (-2.0, -5.1, +0.2) mm; the new offset (+12.0, +12.6, +3.2) mm (the residual subtracted, depth pushed further for E-011) lit it on the next press. Outside, a rim hit (-11.3, -14.8, +0.5) mm gave the offset (+11.3, +14.8, -0.5) mm, but `set_press_offset` failed on its second call in the run (a bug, fixed) and the run was restarted
- What helped: the new offset is the offset in force minus the residual, per axis; on floor buttons, push the depth further than the residual says. The offset in force is the last `set_press_offset` in the history
- Evidence: run_001046, run_235844
- Source: agent trial

### E-021 A longer wait after the walk did not make the call press land closer
- Status: confirmed (press tests, three presses after a walk); cause not known
- Conditions: press tests, outside call panel, down button, right arm, no offset; the walk from the preapproach to the call-panel place, a 15 s hold for an EKF reset done outside the tree (whether it ran is not recorded), then the press
- What was seen: with 35 s of standing still after the walk, before the hold (`press_down_walk_settle`), the fingertip's deepest point was 28.8 mm sideways of its goal; without that wait (`press_down_walk`), 28.3 and 29.8 mm. Presses with no walk before them (`press_down_only`) ended 4.9, 10.9 and 15.6 mm sideways
- Cause: not known. The goal comes from the tracked target, so the sideways number does not say whether the fingertip missed the button
- What helped: not known yet; waiting longer did not
- Evidence: press tests `20261108T202345Z` runs 001, 004, 005, 007 and `20261108T211441Z` runs 004, 005 (`press_reached` numbers in each session's `events.jsonl`)
- Source: press tests, 2026-11-08

### E-022 From one inside stance, floors 1, 2 and 3 were pressed in a row; the fingertip ended within about 1 to 2 cm of each goal
- Status: confirmed (press tests, 39 presses with numbers); whether each button lit is not known
- Conditions: press tests, inside place, right arm, a 5 s hold before each press for an EKF reset done outside the tree; trees `floors_cycle` (offset (+5.0, +7.5, +3.4) mm), `gripper_ab_test` and `tf_ab_*` (no offset)
- What was seen: with the offset (`floors_cycle`, 13 presses) the deepest point was within 7.8 mm short of the goal, and the misses of the 8 mm sideways limit were 8.1 to 10.7 mm. With no offset (`gripper_ab_test`, `tf_ab_*`, 26 presses) 8 presses ended 12.6 to 15.1 mm short and the sideways distance reached 18.1 mm. The A/B trees changed something outside the tree between their halves (gripper correction, TF); which setting gave which numbers is in the session, not in the trees. 4 further presses ended in the 60 s timeout (E-004)
- What helped: the same stance served the three floor buttons; no per-floor stance was needed
- Evidence: press tests `20261108T215835Z` runs 002 to 004, `20261108T222307Z` runs 001, 002, 005, 006, `20261108T233309Z` runs 001 to 004 (`events.jsonl`)
- Source: press tests, 2026-11-08
