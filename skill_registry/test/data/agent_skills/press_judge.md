---
name: press_judge
description: press_button reported success. Look at the frames of the press and judge whether the button was pressed.
runs_when: [press_button succeeded]
runs_on: a Claude Code session through agent_cli (default), or a model inside the executor on the NUC
decisions: [pressed, not_pressed]
---

# Press judge

The tree calls you right after `press_button` reported `Controller reached target`. The arm reaching its goal does not
prove that the button was pressed: the goal can be off the button (a drifting body EKF, a wrong track), or the finger
can stop on the rim. You look at the camera frames of the press and decide `pressed` or `not_pressed`. The tree waits
up to 600 s.

## The evidence

`get_press` (in a session, the `press:` lines of `agent_cli show`, and `agent_cli show --full` for the rest) gives:

- **frames**: JPEG files the executor saved on the NUC (`~/.ros/orchestrator_captures/press_<time>_<name>.jpg`):
  - `baseline`: the first frame once the fingertip reached the pre-grasp, 0.10 m before the button. The button is not pressed yet.
  - `press`: the first frame after the fingertip's **closest point to the button** (the deepest point of the push along the button normal). **This is the frame to judge.**
  - `release`: about 0.5 s after the retreat began.
  - `*_crop`: each frame cropped around track_anything's box of the button, grown 2.5 times. A frame whose own box was hidden by the hand uses the baseline's box.
  A missing frame means that phase was not seen.
- **capture**: `max_depth_m` (the deepest point along the button normal, from forward kinematics; 0 is the goal, negative is short), `lateral_at_max_m` (sideways from the goal there), and the phase times. These measure the arm against its goal, not against the button.
- **failure / history**: the press call itself is the last action in the history.

The camera runs at 6 fps, so the `press` frame can be up to about 0.17 s after the closest point.

## How to judge

1. Say that you are checking the press.
2. Copy the frames to where you can view them (in a session: `scp vector@vector-nuc:<path> .`) and look at `press` and `press_crop` first, then `baseline` and `release`.
3. **Pressed** when, in the `press` frame, the fingertip is on the requested button's face: it covers or touches the centre of that button, not its rim, a neighbour, or the panel beside it. On panels whose buttons light (the floor buttons), a lit ring in `press` or `release` that is not lit in `baseline` is strong evidence; on the freight call panel nothing lights, so judge by where the fingertip sits.
4. **Not pressed** when the fingertip is beside the button, on another button, short of the panel (a gap between finger and panel is visible), or the requested button is not in the frame at all.
5. If the frames are missing or you cannot tell, say so and choose `not_pressed`: the recovery that follows can retry.
6. Say the result, then call `decide` once. Your reason, in two or three sentences: which frame you judged, where the fingertip sat relative to the button, and the depth and sideways numbers.

## In a session

| Tool | In a session |
|---|---|
| `get_press` | `agent_cli show` (frame paths), `agent_cli show --full` |
| `say` | `agent_cli say "..."` |
| `decide` | `agent_cli decide pressed|not_pressed --reason "..." --send` |

The same rules as in `manipulation_recovery.md` ("Rules for a session"): `decide --send` and `say` are the only things you publish. Whether the user's go is needed before `--send` is the user's call for the run.
