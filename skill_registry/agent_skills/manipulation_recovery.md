---
name: manipulation_recovery
description: A press failed or a look before the press failed. Work out why from the evidence, check whether the button was in reach, and decide how the tree should recover.
runs_when: [a look before the first press failed, a press failed or was judged not pressed]
runs_on: a Claude Code session through agent_cli (default), or a model inside the executor on the NUC
decisions: [reposition, switch_arm, offset_retry, retry, ask_help, abort, continue]   # per phase: "Your decision"
---

# Manipulation recovery

The tree calls you in two phases and waits up to 600 s for one decision:

- `stance_check`: the robot walked to a panel and a look (`button_in_view`) failed before the first press.
- `diagnose`: `press_button` failed, or `press_judge` said `not_pressed`.

You cannot move the robot. You read evidence, run your own checks in Python, and end with one decision. The tree carries it out. The executor tells people near the robot when a call opens and what you decided, so you need not narrate.

Your main question: **was the button out of the arm's reach from where the robot stood?** If yes, where should it stand? If no, what else went wrong? Measure and compute; no rule here answers it for you.

**Read as you need them** (same directory):

- `manipulation_recovery.reference.md`: the tools and their `agent_cli` commands, file paths, topics and TF, the press pipeline, pose conventions, arm limits, every failure message, perception and navigation facts.
- `manipulation_recovery.experience.md`: what the team has seen (entries `E-0xx`). Treat each entry as a hypothesis to test.
- "What is known": claims from the knowledge store, with their tier. Inside the executor it is appended below this document. In a session it is not sent: print it with `ros2 run orchestrator knowledge prompt` on the robot computer.

## What the tree does around you

Each panel runs one press slot (`press_panel_button` in `skill_registry/trees/subtrees/press.xml`):

1. **Looks before the first press**, up to 4. A look walks to the goal (first the panel place), rests 2.5 s (the robot settles after it stops), then runs `button_in_view`. That node seeds the tracker, checks the box and 3D pose, and picks the arm by rule: the left arm only when the button is more than `left_arm_min_y_m` to the robot's left in `pelvis` (0.10 m at the inside panel, 0.0 at the call panel), else the right. After each failed look you are called (`stance_check`). If 4 looks fail, the robot asks a person to press.
2. **Presses**, up to 4. Each press runs `set_press_offset`, then `prepare_arm`, then `press_button` and `press_judge`. `prepare_arm` cycles the mode, resets the body EKF (`reset_ekf`: it waits until body_ekf has been still for 2 s and publishes again, at most 10 s), sets the visual-servo profile and resets the target filter. The first press uses offset 0.
3. **After a failed press** you are called (`diagnose`), except for the first small miss, which the executor corrects itself (step 6 below). Then:
   - `offset_retry`, `retry`, `switch_arm`: the tree presses again with your offset or arm.
   - `reposition`: the tree releases the arms, walks to your goal, and runs up to 3 looks. If one succeeds, it presses again. If all 3 fail, **the task ends without asking for help**. Reposition only when you expect the button to be in view from the new stance.
   - `ask_help`: the tree stops pressing and the robot asks a person.
   - `abort`: the task ends at once.

## How to work

1. **Read the failure and the history** (`get_failure`, `get_history`). Note the message, the shape of `distance_to_goal` over time, the offset in force (the last `set_press_offset`), and what this task already tried, including earlier `rule:` answers. For a judged miss, `checked_call` is the press it judged.
2. **Match experience entries** and claims to the symptom. Note which ones you will test.
3. **Find the target.** Use `current_goal` from the failure (`foot_world`). It is already the upright press pose: use it as is. Transform it into `pelvis` with TF; the robot has not moved since.
4. **Check reach.** In `run_python`, load the URDF and `g1.yaml` with pinocchio, apply the active profile's limits, and solve IK for the press pose and the pre-grasp (0.10 m back along the goal's −X). Do it for the arm used, then the other one. Report residuals and joint margins. The arm was chosen by rule; choose `switch_arm` only when your IK shows the rule's arm cannot reach and the other can (E-007).
5. **Read `get_stance`** first: the executor's numbers for where the robot stands, the last press on this button judged pressed with each arm, and `match`, the reposition that puts the target where that press had it, with whether `navigate_to` would walk it. A reposition inside the 0.1 m arrival radius does not move the robot. **Search for a stance** if it is out of reach. Shift the target by candidate base moves and solve again. Arrivals land a few centimetres off (E-001), so pick a stance with margin. If the goal's +X and the pelvis +X differ by more than about 45° in yaw, add a `dyaw` toward the panel.
6. **A press that reached its goal but missed by millimetres** (`press_judge` `not_pressed` after `Controller reached target`) is aim error (E-011/E-012/E-020). Choose `offset_retry` first. New offset = offset in force − `residual_m`, per axis, as the whole offset. On floor buttons push the depth a little further. Check the sign against the press frame. Above 50 mm on any axis is refused: suspect the target or the EKF instead. The executor makes this correction itself, without calling you, on the first diagnosis after a press when the miss in the button plane is 5 to 50 mm. If you are called after that, the history shows its `offset_retry` with a reason starting `rule:`, so the plain correction already failed once.
7. **Weigh the other causes**: perception, a step or EKF drift during the press (E-008, E-009), a slow but progressing reach, a transient rejection. The EKF is reset right before every press, so drift during a press means the reset did not take. For perception, act before asking for help:
   - No track, button in frame: `retry`, up to twice per task (E-004).
   - No track, button large and close: too close for depth; `reposition` back (E-005).
   - Button out of frame or a different one tracked: `reposition` until it is in view (E-003, E-015).
8. **Check the view.** Look at the camera image and project the target into it. A press cannot work on a button the camera cannot see, now or from the stance you choose.
9. **Decide.** Missing or contradictory evidence: `ask_help`. Never guess a reposition.

## Before the first press

A look failed. `get_failure` gives `button_in_view`'s message, which says what was missing after its 10 s wait: no track (button out of frame, E-015, or a random seed failure, E-004), no 2D box, or no 3D pose (too close for depth, E-005). Or the box touched an image edge; `outputs` then has the box and the edge distances. 
A poor stance here comes from the walk. Run steps 3, 4, 5 and 8 of "How to work" on the current stance, then choose:

- `continue`: the stance is fine after all, or it was a random no-seed with the button in frame. The press runs; `press_button` seeds again itself. Choose this when unsure: a failure comes back to you as a diagnosis.
- `reposition`: the tree walks to your goal and looks again. It calls you again only if that look fails too.
- `abort`: as in "Your decision".

The arm is not yours to change here: `button_in_view` chooses it by rule on every look.

## Your decision

| Decision | Phases | When it fits | Fields |
|---|---|---|---|
| `reposition` | diagnose, stance_check | Out of reach or out of view, and another stance fixes it | `dx`, `dy` (m), `dyaw` (rad), relative to the **base goal**, in its frame (x forward, y left). Not cumulative: restate the whole offset each round |
| `switch_arm` | diagnose | IK shows the rule's arm cannot reach and the other can | `use_left_arm` |
| `offset_retry` | diagnose | Goal reached, finger missed by millimetres | `ox`, `oy`, `oz` (m): the whole offset, x deeper, y left, z up, each ≤ 50 mm |
| `retry` | diagnose | Transient cause: rejection, slow reach, no track with button in frame, a step | none |
| `ask_help` | diagnose | Retries and repositions failed, or evidence is missing or contradictory. The robot asks a person to press | none |
| `abort` | all | Button out of view, and no stance you can justify brings it back. Ends the task at once | none |
| `continue` | stance_check | Stance is fine after all, or a random no-seed with the button in frame | none |

- A reposition must be at least 0.25 m from where the robot stands, counting the sideways part (E-002/E-016). Shorter goals end without a step. If you need less, say so and prefer `retry`.
- Do not repeat a reposition that already failed in the same direction. After two failed repositions, choose `ask_help`.
- You have 30 model turns per call and `max_rounds` (5) of your diagnoses per task, shared by both panels. Executor `rule:` answers do not count. After `ask_help` the tree stops calling you.

**Your reason**, in two to four sentences: the evidence (message, feedback shape, target in `pelvis`), what you computed and with which solver, the cause, and why the change should work given arrival error. For `reposition`, give the expected target in `pelvis` after the move. For `abort`, say what you saw and where the button should have been. Cite the entry ids you used and whether this failure fits them.

## Recording what you learn

Never edit the experience file. When this failure shows something new or contradicts an entry or claim:

- Inside the executor: call `note` with one sentence, the `about` tags and the evidence.
- In a session: draft the observation and show it to the user with your decision. It goes in only after the user approves the text.

## When you are a Claude Code session

Run every `agent_cli` command through `scripts/agent_session.py` on the laptop, with the same arguments: `agent_session.py wait`, `agent_session.py tf pelvis foot_world`, `agent_session.py decide ...`. It runs `agent_cli` on the robot computer over ssh (`AGENT_HOST`, default `vector@vector-nuc`) and does two things for you:

- **Frames:** `wait` and `show` copy a judge call's frames to the laptop and print their local paths. Open those paths; never copy frames yourself.
- **Documents:** they sync the skill documents to `~/agent_calls/skills/` and say whether any changed since the last call. Read them at the first call and again only when told they changed.

Start `agent_session.py wait` in the background; it returns when the tree opens a call. Print "What is known" once per run with `ros2 run orchestrator knowledge prompt` on the robot computer. The reference maps each tool to its command. If asked to bring the stack up, follow `docs/operations.md` step by step, with the user's go for each machine.

The same session answers every call in a run, `press_judge` included, so you see earlier calls in your own context. Base each decision on this call's evidence; use what you remember only as a hint to check.

**Rules for a session.** Nothing enforces these for you, so keep them:

- Publish only through `agent_cli decide --send` and `agent_cli say`. Do not call services, send goals, or change files, parameters or configs on any robot computer.
- **Get the user's go before `--send`.** Show the decision, the reason and the evidence. Show it again after any edit.
- Watch the clock in `agent_cli show`. At 600 s the call fails: in `diagnose` the robot then asks a person to press, and in `stance_check` the tree looks again. Warn the user when time is short.
- After each decision, run `agent_session.py wait` again: the next call may be a `press_judge`, another diagnosis, or a stance check at the next panel.
- If `--send` returns `rejected`, fix the decision and show it to the user again.
