# Elevator button-press test runbook (Stanley G1)

How to run the walk-and-press test at the freight elevator, judge each press, and decide the next step. It collects what we learned on 2026-11-04.

## Setup

- **Machines:** the NUC runs the robot interface, arm control, body EKF and navigation. The Orin runs the camera, `seed_bbox` and `track_3d`. The Thor runs Cosmos grounding.
- **Launch:** from the laptop, `utils_ws/src/eigen_vector/scripts/deploy.sh launch nuc map:=/home/vector/maps/two_floor_clean.pcd localization:=true`. Use `SSH_HOST=192.168.123.5` when the laptop is on the robot wire.
- **Check localization first:** `/slam_elevatorlio/status` must say `"mode": "localization"` and `"localized": true`. A stack in mapping mode has no terrain graph, and every nav goal aborts.
- **Places:** `~/maps/two_floor_clean.places.yaml` on the NUC. The inside place sits 10 cm back from the panel (x 3.428, y -0.263, yaw 152.8 deg).
- **Scripts:** this folder. The laptop scripts call NUC copies in `~/` and `/tmp/`, so copy them back after a NUC reboot clears `/tmp`. `cycles4.sh` is the full out-and-in loop; it predates the arm-choice, in-frame and `NO_TRACK` retry rules below, so add those before running it.
- **Code state:** the NUC runs `manipulation_stack` on `test/pelvis-correction` plus uncommitted edits (`final_leg_steps`, `final_leg_settle_time`); a local copy for review is branch `nuc/pelvis-correction-20261104` in `utils_ws/src/g1_pilot`. The NUC `navigation_stack` has the uncommitted path_follower fix and `goal_clear_range` 0.1.

## One press, step by step

1. **Walk.** Send the nav goal. Navigation needs `/stop` at 0, `/autonomy_mode` true and HighLevel mode. Turn autonomy off and return to Idle after arrival.
2. **Check reach before pressing.** Run `prepress.py <target>`. It seeds the button and prints its position in the pelvis frame.
   - **Look at a frame first.** The requested button must be in view. When floor 1 sat below the frame edge, Cosmos grounded floor 2 instead, and the press went to the wrong button.
   - **`NO_TRACK` is often random.** If the button is in frame, rerun the check up to twice before moving the robot.
   - **`NO_TRACK` with a large, close button** means the robot is too close. The D435 gives no depth under about 0.28 m.
   - **Ranges differ by panel.** Outside call panel: 0.33 to 0.48 m ahead (the operator called 0.338 and 0.354 m good). Inside floor panel: about 0.38 to 0.46 m; 0.52 to 0.53 m was too far, and at 0.38 m floor 1 can fall out of frame. Stay within 0.15 m sideways.
   - **Arm choice.** Outside, pick by side (left when y > 0). Inside, use the right arm for floor buttons: the left arm failed IK ("Planning failed: empty trajectory") at y +0.05 to +0.07 m and z 0.22 to 0.24 m above the pelvis. If planning returns an empty trajectory, try the other arm before moving the robot.
3. **Fix the position.** `correct.sh <dx> <dy>` switches to HighLevel, publishes a 0.2 m/s `TwistStamped` on `/local_movement_cmd_vel` for distance / speed seconds, sends zero, and returns to Idle. It is open loop. The mux passes it to `/cmd_vel` and the loco `Move`.
   - **Small moves are unreliable on legs.** An 8.6 cm back move went about 21 cm. Forward moves covered only about half the command (9 cm moved 2 cm, 21 cm moved 10 cm, 20 cm moved about 8 cm).
   - **Too far by 5 cm or more:** command forward about twice the error.
   - **Too close, or slightly too far:** step back 20 cm, then forward. Size the forward leg for the half-coverage, or the robot ends further away than before.
   - **After three failed corrections,** walk out and in again. Resending the same nav goal from where the robot stands can overshoot, and route_planner ignores goals closer than its tolerance.
   - **The walk overshoots the call-panel place by 3 to 6 cm.** `path_follower` `slow_down_distance_threshold` is set live to 0.7 (config default 0.4); a relaunch resets it.
4. **Reset the body EKF.** Run `ekf_reset.sh` once the position is good. The EKF assumes a still robot, and after a walk its yaw drifts up to 0.35 deg/s. That drift swings `foot_world`, which moves the tracked target by centimetres during a press.
5. **Press.** Run `attempt.sh <target> <tag>` with `ARM=left|right`. It bags the press (joints, commands, `/traj`, end-effector and track poses, RGB, aligned depth, lidar), grabs frames at 2 Hz, and prints the residual.
6. **Judge the press** with the three checks below, in order.

## Judge each press, in this order

1. **Did the robot move?** Run `moved.py`. A leg joint swing over 5 deg means a step, and the press is invalid. Retry it with no offset. A step happened only once, at 26.6 deg on the left knee.
2. **Was the tracking right?** Run `trackcheck.py`. Distrust the press when the target wanders more than about 20 mm in `foot_world`, or when torso yaw moves more than about 3 deg. Compare with the camera-frame track: a steady camera track with a moving `foot_world` target points at the EKF, not the tracker.
3. **Where did the fingertip end up?** Run `resid.py`. It prints fingertip minus tracked target in the target frame (x along the button normal, y sideways, z up).
   - Look at the hold frame too. The call-panel indicator does not light, so the image only shows where the gripper sits. The floor buttons light a ring when pressed.
   - Only after checks 1 and 2 pass, set the offset to the negative residual: `ros2 param set /move_to_pose_server arm_safety_distance_{x,y,z}`.
   - **How the offset works:** `to_pelvis_frame_()` in `move_to_pose/planning.cpp` shifts every target by (x, y, z) in the target's own frame (x along the normal, positive deeper). It reads the params at every plan, so a change applies to the next press with no restart. The final-leg stepping freezes the shifted goal in the left-ankle frame, and step 4 lost about 3 mm of a 5 mm depth offset there. The params live only in the running node and reset to 0 on relaunch.
   - **If an offset does not show in the residual,** compare the planned grasp with the reached depth (`depthcheck.py`). A planned depth the fingertip never reaches, with a waist yaw error jump at that moment, means contact, not a missing offset.

## What we learned

- **Most misses were not PID.** A free-air replay of the same arm commands tracked within 0.2 deg. The large errors came from steps, a drifting EKF, the hand meeting the panel, or bad positioning.
- **Repeated zero `/cmd_vel` used to starve the arm.** Each zero twist made `unitree_interface` call a blocking loco `Move`. A timed-out call froze arm commands for 10 s. path_follower now sends one zero and goes quiet (branch `fix/path-follower-idle`, not yet committed).
- **Offsets from a bad press make things worse.** Offsets taken from a press where the robot stepped, or the track drifted, sent the hand further off.
- **The successful floor 1 press** used the right arm at 0.426 m reach, with offset (+6.3, +7.8, +2.9) mm from the previous rim hit.
- **The inside right-arm residual repeats.** Three clean no-offset presses agree within 2 mm, averaging (-5.1, -7.8, -3.7) mm. With offset (+5.0, +7.5, +3.4) mm the sideways and height errors went to +1.6 and +1.2 mm, and the finger sat on the button face.
- **The open problem is pushing force.** With aim fixed, the finger still stops 4 to 7 mm short as waist yaw gives way about 0.5 deg (VisualServo waist kp 200, Ki 60). Self-collision is ruled out in the URDF. Options: a larger depth offset (about +15 mm), or a stiffer or locked waist yaw during the press.
- **body_ekf runs outside the NUC launch** after a reset, in `tmux -L body_ekf`. Stop it before a full relaunch, or two `body_ekf` nodes will run.
- **Stop the robot first, then clean up.** To stop, publish `/stop` 1 and `/autonomy_mode` false in their own command. Never chain them after a `pkill -f` whose pattern also matches your own shell, because that kills the stop command too.
- **Check localization after a bad walk.** The robot once walked off course and hit things because localization had slipped. Confirm `localized` and the inlier ratio before sending the next goal.
- **Status speech helps the operator.** Publish a short `std_msgs/String` on `/speak` with your diagnosis whenever a step fails or a press is skipped.

## Suggestions for lower latency and earlier action

- **Seed while walking.** Send the button query in the last metre of the walk, so the track is live on arrival. The seed alone takes 1.7 to 9 s.
- **Reset the EKF in parallel.** Start `ekf_reset.sh` as soon as the reach check passes. Calibration takes about 2 s.
- **Keep one ROS process alive.** Each `ros2` CLI call spends 1 to 3 s on discovery. A single Python node that walks, checks, sets params and presses would cut that time.
- **Decode on the NUC, not the laptop.** Extract only the small npz per press. Skip the camera topics in the npz.
- **Predict the position problem from the arrival pose.** Arrivals more than about 8 cm from the place pose usually fail the reach check, so correct straight away.
- **Check the EKF yaw rate before every press,** even after a reset. A rate above 0.05 deg/s means the reset did not take.
- **Seed the next offset from history.** Keep a per-panel, per-arm table of clean residuals. Apply its median before the first press, and update it after each clean press.

## Residual log (clean presses only, no offset unless noted)

Fingertip minus tracked target in the target frame: x along the normal (negative = short), y sideways, z up. "Clean" means no step and a steady track.

| Time | Panel | Button tracked | Arm | Reach (m) | x (mm) | y (mm) | z (mm) | Result | Bag |
|---|---|---|---|---|---|---|---|---|---|
| 18:25 | outside | up | left | 0.415 | -7.7 | -12.3 | -4.1 | not confirmed | press_debug_20261104_182519 |
| 18:33 | inside | floor 1 | right | 0.432 | -6.3 | -7.8 | -2.9 | finger on rim | press_debug_20261104_183313 |
| 18:36 | inside | floor 1 | right | 0.426 | -4.3 | -6.7 | -1.5 | pressed, offset (+6.3, +7.8, +2.9) | press_debug_20261104_183646 |
| 18:52 | outside | up | left | 0.413 | -8.7 | -0.2 | -5.1 | not confirmed | press_debug_20261104_185256 |
| 19:07 | inside | floor 2 (asked floor 1) | right | 0.391 | -4.3 | -8.6 | -4.9 | did not press | press_debug_20261104_190703 |
| 19:13 | inside | floor 2 | right | 0.379 | -4.6 | -6.9 | -3.2 | did not press, finger on the right rim | press_debug_20261104_191319 |
| 19:17 | inside | floor 2 | right | 0.380 | -6.5 | +1.6 | +1.2 | not lit; finger on the button face, offset (+5.0, +7.5, +3.4) | press_debug_20261104_191723 |

The 19:07 press asked for floor 1, but floor 1 was out of frame when Cosmos grounded, so it tracked floor 2. The reach check must confirm the requested button is in frame before pressing.

## Self-collision check, floor 2 presses (offline)

`selfcoll.py` runs Pinocchio and coal on the planner's URDF (`g1_29dof_with_hand_rev_1_0.urdf`, convex hulls, adjacent links skipped) at the measured and commanded hold poses. `depthcheck.py` compares planned depth, reached depth and joint error.

- No right-arm hull overlaps anything at the hold. The closest body pair is torso to right shoulder roll link, 19 to 23 mm; the hand and gripper are far from the body.
- The commanded hold pose is just as clear, so the planner did not trim the reach to avoid the body.
- The planner put the depth offset into the plan (+5.7 mm at step 3), but the fingertip stalled 4 to 7 mm short, with waist yaw error jumping to +0.5 deg at that moment.
- So the stall comes from the finger meeting the button, with waist yaw giving way. The URDF hulls rule out self-collision; parts missing from the URDF (cables, gripper mounts) could still touch.
