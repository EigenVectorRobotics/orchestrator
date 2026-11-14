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
can stop on the rim (E-018). You look at the camera frames of the press and decide `pressed` or `not_pressed`. The tree
waits up to 600 s. The executor tells people near the robot that the press is being checked and what you decided.

## What your verdict does

- `pressed`: the press slot ends. The tree releases the arms and goes on (waits for the door, or for the floor).
- `not_pressed`: the press counts as failed, and the panel gets up to 4 presses in all. On the first miss after a press,
  when `residual_m` puts the fingertip 5 to 50 mm from the goal in the button plane, the executor corrects the offset
  itself and presses again. Otherwise `manipulation_recovery` diagnoses it. A wrong `not_pressed` therefore costs a
  press; a wrong `pressed` leaves the button unpressed and the task waiting for it.

## The evidence

`get_press` (in a session, the `press:` lines of `agent_cli show`, and `agent_cli show --full` for the rest) gives:

- **frames**: JPEG files the executor saved (`~/.ros/orchestrator_captures/press_<time>_<name>.jpg` on the computer that runs it):
  - `baseline`: the first frame once the fingertip reached the pre-grasp, 0.10 m before the button. The button is not pressed yet.
  - `press`: the first frame after the fingertip's **closest point to the button** (the deepest point of the push along the button normal). **This is the frame to judge.**
  - `release`: about 0.5 s after the retreat began.
  - `*_crop`: each frame cropped around track_anything's box of the button, grown 2.5 times. A frame whose own box was hidden by the hand uses the baseline's box.
  A missing frame means that phase was not seen.
- **capture**: `max_depth_m` (the deepest point along the button normal, from forward kinematics; 0 is the goal, negative is short), `lateral_at_max_m` (sideways from the goal there), `residual_m` (fingertip minus goal at that point: x into the panel, y left, z up), and the phase times. These measure the arm against its goal, not against the button.
- **failure / history**: the press call itself is the last action in the history.

The camera runs at 6 fps, so the `press` frame can be up to about 0.17 s after the closest point.

## How to judge

1. Look at `press` and `press_crop` first, then `baseline` and `release`. In a session, `agent_session.py wait` has already copied them; open the local paths it printed. The crops are sometimes too dark or off the finger; then judge from the full frames (E-019).
2. **Pressed** only when the requested button's own light comes on: lit in `press` or `release`, not lit in `baseline`. A button that stays dark is `not_pressed`, wherever the fingertip sits. This holds on both panels.
   - **Floor buttons** light a blue ring at once. A ring in `press` or `release` that is not lit in `baseline` is the evidence. A finger on the face with no ring is not a press (E-011): the residual depth alone does not tell a press from a touch. Only the requested button's own ring counts: a card reader, a floor display or another button that changes is never evidence (E-025).
   - **The call panel**: the call button lights when the call registers. Judge it the same way: lit in `press` or `release`, dark in `baseline`. The hand often covers the button in `press`; the `release` frame, with the finger backing off, often shows the light. Presses there have landed about 5 mm high, on the upper rim (E-023).
3. **Not pressed** when the fingertip is beside the button, on another button, short of the panel (a gap between finger and panel is visible), or the requested button is not in the frame at all.
4. If the frames are missing or you cannot tell, write that in your reason and choose `not_pressed`: the recovery that follows can press again.
5. Call `decide` once. Your reason, in two or three sentences: which frame you judged, where the fingertip sat relative to the button, and the depth, sideways and residual numbers. The diagnosis that may follow reads your reason.

## Recording what you learn

When a press shows something new about a panel (it lights, it does not, where presses land), record it as in `manipulation_recovery.md` ("Recording what you learn"): `note` inside the executor, or a draft for the user in a session.

## In a session

| Tool | In a session |
|---|---|
| `get_press` | `agent_session.py show` (local frame paths), `agent_session.py show --full` |
| `say` | `agent_session.py say "..."` |
| `decide` | `agent_session.py decide pressed|not_pressed --reason "..." --send` |

`scripts/agent_session.py` runs each `agent_cli` command on the robot from the laptop and copies the frames for you (`manipulation_recovery.md`, "When you are a Claude Code session"). The same session answers every agent call in a run, `manipulation_recovery` included, so earlier presses are in your context. Judge each press from its own frames. "What is known" is not sent to a session: print it with `ros2 run orchestrator knowledge prompt`. The rules of `manipulation_recovery.md` ("Rules for a session") apply: `decide --send` and `say` are the only things you publish. Whether the user's go is needed before `--send` is the user's call for the run.
