# Navigation: experience

What the team has seen on the robot about walking, localization and the processes that navigation needs: symptoms, causes, and what helped. No agent skill reads this file yet; `docs/troubleshooting.md` points to its entries, and a navigation recovery skill would read it the way `manipulation_recovery` reads `manipulation_recovery.experience.md`.

Facts that hold by construction (code, configs) belong in the skill documents and the contracts (for navigation: "Navigation facts" in `manipulation_recovery.reference.md`). This file holds what was observed: it can depend on conditions, and an entry can be wrong.

Some navigation observations are in `manipulation_recovery.experience.md`, because the press recovery uses them, and stay there: E-001 (where a walk stops), E-002 and E-016 (a goal close to the robot ends without a step), E-013 (local moves), E-017 (latched terrain points block the walk into the cabin).

## How an entry is written

```
### N-<number> <the symptom, as it shows in the evidence>
- Status: confirmed (n times) | reported | suspected
- Conditions: where and with what it was seen (place, software versions)
- Cause: what caused it, and how that was checked
- What helped: the decision or fix, and the result
- Evidence: trace, bag, log, issue or file
- Source: who saw it and when; who confirmed it
```

- **confirmed**: seen on the robot and the cause checked, at least once; n counts the times.
- **reported**: stated by a teammate or written in a repository, not yet checked in a recorded run.
- **suspected**: a cause someone proposed from one run; not checked.

**Who writes.** A person, or an agent that drafts an entry and has a person approve the exact text before it goes in. An agent never adds or edits entries on its own. An entry that turns out to hold by construction moves to the skill document or the contract as a fact and leaves this file.

## Entries

"The field test" and "the agent trial" are those of 2026-11-04, as in `manipulation_recovery.experience.md`.

### N-001 New processes cannot join DDS: goals, probes and trees never start
- Status: confirmed (agent trial; again on 2026-11-08)
- Conditions: the NUC, unitree_sdk2's CycloneDDS 0.10.2; the stack alone runs about 25 processes, plus the executor, the agent session's CLI, the ros2 daemon, CLI calls, and on 2026-11-08 the recorder's bag
- Signs: a new process fails with `Failed to find a free participant index for domain 0`; a nav goal, a probe or `send_tree` never starts, so a walk looks like it routes but never moves
- Cause: CycloneDDS gives each process an automatic participant index from a limited range (32 by default), and the range was full. Orphaned processes hold an index too (on 2026-11-04 an orphaned `ros2 topic echo /orchestrator/trace`)
- What helped: `<Discovery><MaxAutoParticipantIndex>120</MaxAutoParticipantIndex></Discovery>` in `bringup/config/cyclonedds.xml` (eigen_vector); a process reads it when it starts. On domain 99 with 50 participants, 18 failed with the old file and none with the new one. Kill leftover processes (`ss -ulpn | grep -E ':74[0-9][0-9] '` gives one pair of ports per process; a process whose parent is `init` is a leftover)
- Evidence: `~/orch_runs/20261108T185515Z-07dfe9/RUNS.md` on the NUC ("Fixes made before these runs")
- Source: agent trial, 2026-11-04; Jinyao, 2026-11-08. The file change is deployed on the NUC, not committed (`docs/deployment.md`)

### N-002 Something standing within about 0.3 m of the robot blocks every path
- Status: confirmed (field test)
- Conditions: walks near the elevator with people, a stand or the gantry close to the robot
- Signs: `local_planner` publishes only one-pose stop paths on `/path`, `/free_paths` is empty, `path_follower` sends nothing; `/terrain_map` (intensity is the height above ground; above 0.1 m blocks) holds points close around the robot
- Cause: real obstacles within about 0.3 m of the robot. Latched points left in the terrain map give the same signs (E-017)
- What helped: clear the terrain map (`/clear_terrain_map`, `std_msgs/Empty`): latched points go, and points that come back are real, so move the obstacle or the robot
- Source: field test notes, 2026-11-04

### N-003 Repeated zero `/cmd_vel` froze the arm for 10 s
- Status: confirmed (field test)
- Conditions: the robot standing after a walk, `path_follower` sending zero twists
- Cause: each zero twist made `unitree_interface` call a blocking loco `Move`; a call that timed out froze arm commands for 10 s
- What helped: `path_follower` sends one zero and then goes quiet (navigation_stack branch `fix/path-follower-idle`, not committed at the time). [TODO: check whether it is merged and deployed]
- Source: field test notes, 2026-11-04

### N-004 A walk went off course and hit things after localization had slipped
- Status: reported (field test, once)
- Conditions: a walk started after a poor walk, without checking localization
- What helped: before the next goal, check that `/slam_elevatorlio/status` says `"localized": true` and that the inlier ratio is high
- Source: field test notes, 2026-11-04

### N-005 With no initial pose, the first match can land at the edge of the yaw search window
- Status: confirmed (agent trial, twice; 2026-11-08, once rejected)
- Conditions: localization started without `/initialpose`; it assumes the robot near the map origin and searches yaw within +-30 deg
- What was seen: in the agent trial both launches matched on their own at yaw -30.1 and -25.7 deg, near the edge; both passed a scan fit check (0.76 and 0.80 of the scan within 10 cm at 0 deg, 0.56 to 0.66 at +-10 deg; a later check in the cabin gave 0.79). A match at -33.7 deg, outside the window, was suspect. On 2026-11-08 the first match was rejected (structure inliers 0.28), and the operator sent `/initialpose`; the relocalization in place that followed moved 0.01 m with structure inliers 0.95
- What helped: a right match has a median about 3 cm, 75 to 80 % of points within 10 cm, and the share falling when the scan is turned 5 or 10 deg either way. Look whether `/registered_scan` lies on `/route_planner/terrain` in Foxglove; send `/initialpose` when it does not
- Evidence: `~/orch_runs/20261108T185515Z-07dfe9/RUNS.md` on the NUC (run 001)
- Source: agent trial, 2026-11-04; Jinyao, 2026-11-08

### N-006 Tracking alone does not recover the pose after a ride; a relocalization in place does
- Status: confirmed (2026-11-07 once; 2026-11-08 runs)
- Conditions: walking out of the freight elevator cabin after a ride, navigation_stack `jean/elevator-side`
- What was seen: on 2026-11-07 the pose at the exit of the floor-2 cabin was 0.5 m off; tracking rejected every correction outside the cabin and SLAM went lost. In the runs of 2026-11-08 the robot stopped about 1 m out of the door and relocalized in place (`relocalize_here`): it moved 0.02 m with structure inliers 0.97, and the walk to the target went on
- Cause: a re-search about the current pose accepts a larger correction than tracking does; the cabin itself matches poorly
- What helped: stop about 1 m out of the door and run `relocalize_here` before walking on (`freight_elevator_go_to_place`, subtree `walk_out`)
- Evidence: `skill_registry/contracts/relocalize_here.yaml`; `~/orch_runs/20261108T185515Z-07dfe9/RUNS.md` on the NUC (run 002)
- Source: Jinyao, 2026-11-07 and 2026-11-08

### N-007 After a LIO divergence, SLAM stays unhealthy until it is restarted
- Status: confirmed (2026-11-07, once)
- What was seen: a LIO divergence made `/registered_scan` garbage, and `door_state_node` said OPEN at a closed door. `slam_healthy` stays false for good after a divergence; `/initialpose` does not clear it
- What helped: restart SLAM (the NUC stack). The trees' SLAM guard halts a task on unhealthy SLAM outside the cabin
- Evidence: `skill_registry/contracts/slam_healthy.yaml`
- Source: Jinyao, 2026-11-07

### N-008 The robot turned back and forth in place; LIO drifted and the map match was refused until SLAM went lost
- Status: suspected (once, 2026-11-08); analysed from the bag by a subagent, not re-checked
- Conditions: the walk from the call panel into the cabin, started 1 s into `release_arms` (`freight_elevator_ride` before the fix); navigation_stack `jean/elevator-side`
- What was seen: the base did not move during the release, then turned back and forth at about 1 rad/s for about 40 s next to the wall without progressing. LIO drifted about 0.8 m in about 3 s; every map match after that wanted a correction above `max_tracking_correction_m` (0.3 m) and was rejected, with inliers still 0.99; after 10 s with no accepted match SLAM reported lost (`healthy: false`) and the tree's SLAM guard failed the task. The old pose was about 1.4 m off afterwards; a restart of the navigation launch localized correctly (confirmed by the operator)
- Cause: suspected: the fast turning made LIO drift; why the follower turned in place is not known
- What helped: when the robot keeps turning in place instead of walking, stop it and ask a person to send `/initialpose` from Foxglove; if matches stay rejected, restart the navigation launch. The tree now releases the arms before the walk in
- Evidence: bag `~/orch_runs/bags/full_test_20261108_214213` (deleted afterwards); `~/orch_runs/loc_fail_20261108/` (`nav_launch.log`, `nav_pane.txt`); session `20261109T024122Z-d5bad5` run 001
- Source: test run, 2026-11-08; subagent analysis
