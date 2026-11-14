# Manipulation recovery: experience

What the team has seen about press failures, their causes, and what helped. Each entry depends on its conditions and can be wrong. Facts that hold by construction go in `manipulation_recovery.reference.md`.

## Format

```
### E-<number> <the symptom, as it shows in the evidence>
- Status: confirmed (n) | reported | suspected
- Seen: conditions and numbers
- Cause:
- What helped:
- Evidence: runs, sessions, bags
```

- **confirmed**: seen on the robot with the cause checked. **reported**: stated by a teammate, not checked in a run. **suspected**: proposed from one run.
- A person writes entries, or approves an agent's draft word for word. An agent never edits this file.
- Merged entries keep every old id, so references elsewhere stay valid.

**Test sets.** *Field test*: 2026-11-04, Stanley G1, freight elevator call panel (outside) and floor panel (inside). *Agent trial*: the full task on 2026-11-04 to 05, answered by a Claude Code session; traces in `~/orch_runs/run_*/trace.txt`, frames in `~/.ros/orchestrator_captures/`. *Press tests*: 2026-11-08 test trees with `press_reached` and no stance check; sessions `20261108T185515Z` to `20261108T233309Z`.

## Stance and walking

### E-001 / E-015 Arrivals land 3 to 24 cm off the place; too far past, the button drops below the frame
- Status: confirmed (field test; agent trial, 6 arrivals)
- Seen: with the slow-down threshold at 0.7, call-panel arrivals overshot by 3 to 6 cm. At the default 0.4 they ranged from 1 cm short to 17 cm past at the call panel and up to 24 cm past inside, with up to 10 cm sideways and 9° yaw. Reposition walks of 0.27 and 0.29 m ended 7 and 8 cm off their goals
- Signs when too close: `press_button` times out with `distance_to_goal` inf; `ground_server` returns 0 boxes; the camera shows the panel large with the button at or below the bottom edge
- Cause: the robot stops once inside the arrival thresholds. The camera looks down, so a closer robot sees the button lower in the frame
- What helped: `reposition` back (a goal under 0.10 m away is walked to from 0.15 m behind it). In good frames the down button sat near y 540 of 720 at the call panel, and floor 1 near y 450 to 600 inside
- Evidence: agent trial run_235411, run_235844, run_000131, run_001046

### E-002 / E-016 A reposition shorter than about 0.25 m ends at once, without a step
- Status: superseded on 2026-11-16: the code now takes 0.10 m as the shortest walk (`WALK_MIN_M`) and walks 0.15 m behind a shorter goal first (E-024). The 0.25 m below may no longer hold; kept as the history of the 2026-11-04 trial (agent trial, 2; cause of the 0.18 m case suspected)
- Seen: repositions 0.148 m and 0.18 m away succeeded in 0.2 s with no step. Those 0.27 and 0.29 m away walked
- Cause: below 0.15 m the goal is already inside `goal_reached_threshold`. For 0.18 m, suspected: a stale `/goal_reached` or the follower's tolerance
- What helped then: keep a reposition at least 0.25 m from `/odom_base_link`, counting the sideways part. For a smaller correction, walk out and back in (tree `freight_elevator_floor1_walk_in_again_agentic.xml`: 0.35 m behind, then the place). After it the robot stood within 5 cm of the inside place and reached floor 1
- Evidence: run_235411, run_000131, run_001046

### E-013 Open-loop local moves are unreliable for small corrections
- Status: confirmed (field test)
- Seen: forward moves on `/local_movement_cmd_vel` covered about half the command; an 8.6 cm back move went 21 cm. The tree has no local-move skill
- What helped: walk out and back in instead (E-002/E-016)

### E-024 A short reposition backed the robot 0.35 m off, and the press ran from there
- Status: confirmed (once, 2026-11-13)
- Seen: inside, floor 1, right arm. At the inside place the robot stood 8 to 9 cm short, with the button 0.548 and 0.560 m ahead of the pelvis (the pressed reference: 0.507 m). IK missed by 18 and 47 mm. The matching stance was 6 to 8 cm away, inside the arrival radius, so the agent set dx -0.34 and -0.36 m, as its prompt said: back off now, walk in at the next call. The tree presses after every reposition, so presses 2 and 4 ran from 0.31 and 0.33 m short (button 0.78 and 0.82 m ahead, IK 229 and 267 mm off). The task used all 5 diagnoses and failed
- Cause: the back-off and the walk-in were split across two rounds, but every round ends in a press. The inside place leaves 4 to 5 cm of reach, less than the arrival spread (E-001)
- What helped: the executor now outputs `walk_in` and `approach`. For a reposition goal under 0.10 m from the robot, the look walks 0.15 m behind the goal and then onto it, in one round. Not yet tested on the robot. Moving the inside place about 4 cm toward the panel would add margin; not done
- Evidence: session `20261114T002938Z-9358f6` run 004, agent calls 71f1552f, 1241d652, 372b850e and de8db370, and its bag

## Perception

### E-003 The seed locks onto a different button
- Status: confirmed (field test, 1; 2026-11-08, 3)
- Seen: inside, with floor 1 below the frame, Cosmos grounded floor 2 and the arm pressed it. On the floor 1 hall panel, `up button` grounded the black light above the grey button, while `down button` found the grey button
- Cause: the grounding model returns a plausible box even when the object is absent
- What helped: confirm the requested button is in frame before pressing; move until it is. On the floor 1 hall panel, ask for `down button`. Walking closer does not fix a wrong target
- Evidence: session `20261109T031406Z` runs 002, 005, 006

### E-004 No track is often random
- Status: confirmed (field test; press tests, 10 of 69 presses timed out)
- Seen: the seed takes 1.7 to 9 s. Most timeouts had almost no tracked target, though `ground_server` answered every request
- Cause: not known
- What helped: with the button in frame, retry up to twice before moving the robot

### E-005 No track with a large, close button: too close for depth
- Status: confirmed (field test)
- Cause: the D435 gives no depth under about 0.28 m
- What helped: step back

## Reach and arm

### E-007 Which arm: by side outside, the right arm inside
- Status: confirmed (field test)
- Seen: outside, the arm on the button's side worked (left when y > 0 in `pelvis`). Inside, the left arm failed IK for floor buttons at y +0.05 to +0.07 m; the right arm reached them
- What helped: on a planning failure, try the other arm before moving the robot

### E-018 `Controller reached target` while the fingertip stopped 38 to 67 mm short
- Status: confirmed (agent trial, 1; press tests, 2)
- Seen: inside, floor 1, robot 13 cm behind the place (about 0.55 m to the button): the action succeeded in 15 s, 67 mm short and 39 mm beside the goal, with no press frames. Two press-test walk-ins ended 38 and 49 mm short
- What helped: judge every press from frames and numbers. A press that ends far short is out of reach: reposition closer
- Evidence: run_000131; press tests `20261108T211441Z` run 013, `20261108T215835Z` run 001

### E-022 One inside stance served floors 1, 2 and 3
- Status: confirmed (press tests, 39 presses); lighting not recorded
- Seen: with offset (+5.0, +7.5, +3.4) mm, the deepest point was within 8 mm short and 8 to 11 mm sideways. With no offset, up to 15 mm short and 18 mm sideways
- What helped: no per-floor stance is needed

## Judging and aim

### E-008 The body EKF's yaw drifts after a walk and drags `foot_world`
- Status: confirmed (field test)
- Seen: up to 0.35°/s after a walk, so the target moves by centimetres in `foot_world` during a press while the camera-frame track stays steady
- What helped: reset the EKF at the panel before the press (the tree's `ekf_hold`). A yaw rate above 0.05°/s after a reset means it did not take

### E-009 / E-010 Check a press in order: step, track, then fingertip
- Status: confirmed (field test)
- Seen: a leg joint swinging more than 5° means the robot stepped; the press is invalid, so retry it with no correction. Distrust a press whose target moved more than 20 mm in `foot_world` or whose torso yaw moved more than 3°. Only then read the residual. A free-air replay tracked within 0.2°, so large misses came from steps, EKF drift, contact or stance, rarely from the controller
- What helped: take corrections only from presses that pass the first two checks

### E-011 / E-012 / E-020 The aim error repeats per panel and arm; `offset_retry` fixes it
- Status: confirmed (field test; agent trial, 1 inside)
- Seen, inside, right arm: clean presses landed 5 to 8 mm short, sideways and low. The fingertip also stalls 4 to 7 mm short as the waist yaw gives about 0.5° on contact. Floor 1 hit the rim with no offset. With (+5.0, +7.5, +3.4) mm it lit once and touched unlit once. An `offset_retry` to (+12.0, +12.6, +3.2) mm then lit it at once
- Seen, outside, right arm, down button: residuals (−11.3, −14.8, +0.5) and (−8.7, +4.0, −0.3) mm. The sideways sign changed between presses, so there is no fixed outside offset yet
- Cause: a repeatable aim error, plus the waist stall on floor buttons. Self-collision is ruled out on the URDF hulls
- What helped: new offset = offset in force − residual, per axis, from clean presses only. On floor buttons push the depth a little further. The residual depth alone does not tell a press from a touch (lit at −6.2 mm, unlit at −2.0 mm)
- Evidence: run_234904, run_235844, run_001046; bags `press_debug_20261104_*`

### E-019 Judge from the press frame: a press counts only when the button lights
- Status: confirmed (agent trial, 8 presses)
- Seen: floor buttons light a blue ring at once, still visible 0.5 s later in the release frame. The call button lights when the call registers; the hand often covers it in the press frame, and the release frame, with the finger backing off, shows the light. Crops were sometimes too dark; full frames decided
- What helped: use the button's light as the answer, on both panels, and `residual_m` as a cross-check

### E-021 Waiting longer after the walk did not improve the call-panel press
- Status: confirmed (press tests, 3 presses); cause not known
- Seen: after a walk, presses ended 28 to 30 mm sideways of the goal, with or without an extra 35 s wait. Presses with no walk before them ended 5 to 16 mm sideways
- Cause: not known. The goal comes from the track, so this number does not show whether the button was missed

### E-023 Outside, the press lands about 5 mm high and the gripper hides the button
- Status: reported (operator, 2026-11-08); the missing gripper correction is confirmed in logs
- Seen: the fingertip hit about 5 mm above centre, often on the upper rim. move_to_pose ignored a stale gripper estimate outside, likely because the gripper was below the frame; inside it applied about 16 mm
- What helped: not solved. When the gripper hides the button, weigh `residual_m` z, the baseline frame and the hand's position. If presses keep landing high, try `offset_retry` with oz about −0.005 and the other axes from the residual

## Resolved

- **E-014** `press_reached` reported 19 to 23 cm sideways misses after good presses. The phase tracker took the arm's swing up for the push. Fixed in `press_capture.py` on 2026-11-05.
- **E-017** Latched terrain points blocked the walk into the cabin. Every walk in the trees now runs `clear_terrain_map`, up to three times.
