"""The text the model sees for each agent call: the system prompts and the prompt built from the call.

The decisions, the fields and the limits are not written here: they come from agent_spec (shared with the executor) and,
for each call, from the call itself (its phase, button, decisions, limits and offset in force), so the prompt states what
the executor will check. The numbers below are the model's own guidance, which the executor does not check.
"""

import math
import sys

from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parents[1] / 'orchestrator'))   # agent_spec, which needs no ROS

from orchestrator import agent_spec as spec                                         # noqa: E402


JUDGE_FRAMES      = ('press', 'release', 'baseline')
DETECTION_JUMP_M  = 0.10                # a button that moved more than this since its reference is a suspect detection
BACK_OFF_M        = 0.15                # the Back-off stance: this far behind the robot now
SIDE_SHIFT_M      = 0.12                # S3: the shift toward a button at the image edge
NOMINAL_TARGET_M  = {False: (0.48, -0.06), True: (0.40, 0.05)}     # target in pelvis of presses that worked, by left_arm
REACH_AHEAD_MAX_M = 0.52
QUIET_SKILLS      = {'set_mode', 'reset_ekf', 'set_profile', 'reset_filters', 'set_autonomy', 'clear_stop', 'say', 'release_arms'}

JUDGE_SYSTEM = '''You judge one elevator button press by a humanoid robot. The question: is the requested button pressed \
now? Answer pressed or not_pressed.

Evidence you get
- Frames from the head camera, looking down at the panel. baseline: fingertip 0.10 m before the button, nothing \
pushed yet. press: fingertip at its deepest point; judge it first. release: about 0.5 s after the retreat began. \
A crop is a zoom around the tracked button. Each frame is attached as an image, right after its label.
- The panel type, stated in the call. Car panel: floor buttons ("3", "2", "*1"), each with its own light ring. \
Call panel (outside the car): the call button, with its own light.
- Arm numbers. They measure the arm against its goal, not against the panel, so they never decide alone.

Rules, in order
1. Both panels: the requested button's own light decides. Lit in press or release: pressed. Dark in both: \
not_pressed, wherever the fingertip sits.
2. A light already on in baseline means the call or floor is registered: pressed. A missing baseline does not weaken \
a lit button.
3. Only the requested button's own light counts. The card reader (a bar light left of the buttons that turns red or \
green), the floor display and other buttons are never evidence.
4. When no frame shows the requested button's own light (the button or the fingertip is out of view, or the press and \
release frames are missing): not_pressed. The recovery can press again; a wrong pressed leaves the task waiting.

Reason, three short sentences: (1) the requested button's light in baseline, press and release; (2) where the fingertip sits and in which frame; (3) the rule number that decided.'''

def recovery_system(limits: dict) -> str:
    """The recovery's rules, with the limits the executor enforces (agent_spec.limits())"""
    move      = limits['reposition_m']
    reachable = move['forward'] + REACH_AHEAD_MAX_M

    return f'''You choose how a humanoid robot (Unitree G1) recovers at an elevator panel. The call gives the \
phase and its question:
- diagnose: a press failed, or press_judge said not_pressed. Why, and what should the tree do next?
- stance_check: the look before the first press failed (button_in_view or check_reach). Is the stance fine, or where \
should the robot stand?
Answer with one allowed decision, its fields, and a reason of two to four sentences that quotes the numbers you used \
and names the step below that decided.

Frames and units (lengths in m, angles in rad)
- Base-goal frame: the panel stance from the map; x forward toward the panel, y left. dx, dy, dyaw state the whole \
stance in this frame, not a step from where the robot stands. navigate_to drives base_link there; after it stops the \
robot can still settle by up to about 0.065 m and 0.19 rad. A robot more than {limits['stance_tolerance_m']:g} m or \
{limits['stance_tolerance_rad']:g} rad off the stance it was sent to is sent there again by the executor before you are asked.
- Pelvis frame: the robot body; x forward, y left, z up. "Target in pelvis" comes from the press goal in foot_world, \
with EKF drift up to 0.03 m. A reach miss is arm end minus target, in pelvis: x < 0 means the arm stops short.
- "Stance now" is the pelvis against the base goal. "Button in base-goal frame" is computed for you: stance now plus \
the target rotated by the stance yaw. The panel does not move, so this point must agree across calls within 0.05 m.
- Offset (ox, oy, oz): added to the press goal; x into the panel, y left, z up. State the whole offset, not a change.

Hard rules
1. Limits: -{move['back']:.2f} <= dx <= +{move['forward']:.2f} and |dy| <= {move['side']:.2f}. The executor refuses more. \
A button that needs more means a wrong detection, not a wrong stance.
2. Give the stance where the robot should stand. The executor plans the walk: navigate_to takes no step for a walk under \
{limits['walk_min_m']:g} m, so such a stance is reached by walking out {limits['approach_back_m']:g} m behind it first, then \
onto it. Never add that walk-out to dx yourself. (The Back-off stance of S2 is a stance you choose, not this walk-out.)
3. Diagnose after a passed check_reach: a reposition is refused unless the target moved {limits['stance_changed_m']:g} m or \
more since the check.
4. Each offset axis within {limits['press_offset_max_m']:g} m.
5. switch_arm only when "Other arm" shows a press judged pressed within 0.05 m of this target.
6. A press of another button, or at another panel, is never a stance source.
7. Presses that worked had the target 0.37-0.52 m ahead of the pelvis (right arm 0.45-0.52, left arm 0.37-0.42). \
So a button more than {move['forward']:.2f} + {REACH_AHEAD_MAX_M:.2f} = {reachable:.2f} m ahead in the base-goal frame cannot \
be reached from any allowed stance.

Diagnose: take the first step that fits
D1 Detection check. "Detection" says suspect (the button moved over {DETECTION_JUMP_M:g} m from the reference press, or sits \
beyond {reachable:.2f} m ahead): retry if the history has no retry since the last walk, else ask_help. Never reposition toward it.
D2 Aim miss. press_judge said not_pressed after "Controller reached target" and the suggested offset is within limits: \
offset_retry with that offset. Out of limits: go on to D3.
D3 Reach. "target out of reach" (or an aim miss too large for an offset):
  a. Reach check passed and the target moved under {limits['stance_changed_m']:g} m: retry once; if a retry already failed \
the same way, ask_help.
  b. Otherwise reposition to "Match" (same button and arm) when listed, else to "Nominal". Use it only when it is \
inside the limits; else ask_help.
D4 Repeats. Two repositions already failed in this task, or yours repeats a failed one within 0.05 m: ask_help.
D5 Tracking. No track or lost track with the button in frame: retry, at most twice per task.
D6 Nothing fits, or the evidence contradicts itself: ask_help.

Stance check: take the first step that fits
S1 Reach check failed. "Reach move" fixable and within limits: reposition to it. Within limits false: the detection \
is suspect; continue. Fixable false: continue. (continue looks again from here: button_in_view and check_reach must both \
pass before any press.)
S2 Too close. The box touches the bottom edge (centre y >= 0.90 of the image, or under 36 px to the bottom), or there \
is no 3D pose because it is too close for depth: reposition to "Back-off stance" ({BACK_OFF_M:g} m behind the robot now).
S3 Off to the side. The box touches the left or right edge: reposition to "Match" or "Reference stance" when it is \
{limits['walk_min_m']:g} m or more from the robot, else shift dy {SIDE_SHIFT_M:g} m toward the button.
S4 No track or no box, but the button is in the camera image: continue (the look runs button_in_view again from here).
S5 Button not in the camera image and no stance you can justify: abort.'''


RECOVERY_SYSTEM = recovery_system(spec.limits())      # the bridge's warm model; a call with other limits gets its own


def judge_prompt(request: dict, frames: set) -> str:
    press   = request.get('press') or {}
    capture = press.get('capture') or {}
    button  = request.get('button', '')
    panel   = panel_type(button)
    shown   = judge_images(frames)
    missing = [name for name in JUDGE_FRAMES if name not in frames]
    lines   = [f'Button: {button}. Panel: {panel}.', f'Arm: {"left" if press.get("left_arm") else "right"}.',
               f'Frames attached below: {", ".join(shown) or "none"}.' + (f' Missing: {", ".join(missing)}.' if missing else '')]

    lines.append(f'Arm against its goal, not the panel: deepest {fmt(capture.get("max_depth_m"))} m along the button '
                 f'normal (0 is the goal, negative is short), {fmt(capture.get("lateral_at_max_m"))} m sideways there.')

    return '\n'.join(lines)


def judge_images(frames: set) -> list[str]:
    """The judge's images in order: each frame, then its crop"""
    return [name for frame in JUDGE_FRAMES for name in (frame, f'{frame}_crop') if name in frames]


def recovery_prompt(request: dict, camera: bool) -> str:
    stance  = request.get('stance') or {}
    history = request.get('history') or []
    phase   = request['phase']
    limits  = call_limits(request)
    button  = request.get('button', '')
    lines   = [f'Phase: {phase}. Button: {button} ({panel_type(button)}).',
               f'Allowed decisions: {", ".join(request["decisions"])}.']

    if request.get('base_goal'):
        goal = request['base_goal']
        lines.append(f'Base goal (map): x {fmt(goal.get("x"))}, y {fmt(goal.get("y"))}, yaw {fmt(goal.get("yaw", 0.0))}. '
                     'A reposition is relative to it, not to where the robot stands.')

    lines += [''] + failure_lines(request.get('failure') or {}, request.get('press'), request.get('offset_in_force') or [0.0] * 3, limits)
    lines += [''] + stance_lines(stance, phase, limits)
    lines += [''] + reach_lines(stance.get('reach'), stance.get('reach_move'), limits)
    lines += [''] + history_lines(history)

    if camera:
        lines += ['', 'Camera image now: attached below.']

    return '\n'.join(lines)


def call_limits(request: dict) -> dict:
    """The limits the executor enforces, as the call states them"""
    return request.get('limits') or spec.limits()


def failure_lines(failure: dict, press: dict | None, offset: list, limits: dict) -> list[str]:
    call    = failure.get('checked_call') or failure
    capture = (press or {}).get('capture') or {}
    lines   = ['## Failure', f'- {failure.get("skill")} {failure.get("status")}: {failure.get("message")}']

    if failure.get('checked_call'):
        lines.append(f'- Judged press: {call.get("skill")} {call.get("status")}: {call.get("message")}')

    outputs = failure.get('outputs') or {}

    if failure.get('skill') == 'button_in_view' and outputs.get('centre_frac'):
        lines.append(f'- Box centre (x, y) as image fractions: {outputs["centre_frac"]}; pixels to each edge: '
                     f'{outputs.get("edge_px")}; image {outputs.get("image_size")}')

    lines.append(f'- Arm: {"left" if (call.get("inputs") or {}).get("left_arm") else "right"}. Offset in force '
                 f'(x in, y left, z up): {vec(offset)} m')

    if capture:
        residual  = capture.get('residual_m') or [0.0, 0.0, 0.0]
        suggested = [round(o - r, 4) for o, r in zip(offset, residual)]
        inside    = all(abs(value) <= limits['press_offset_max_m'] for value in suggested)
        lines += [f'- Press capture: deepest {fmt(capture.get("max_depth_m"))} m along the normal (negative is short), '
                  f'residual fingertip minus goal (x in, y left, z up) {vec(residual)} m',
                  f'- Suggested offset (offset in force minus residual): {vec(suggested)} m, '
                  f'{"within" if inside else "OUTSIDE"} the {limits["press_offset_max_m"]:g} m limit']
    else:
        lines.append('- Press capture: none (the press did not run)')

    return lines


def stance_lines(stance: dict, phase: str, limits: dict) -> list[str]:
    now    = stance.get('stance_from_base_goal')
    target = stance.get('target_in_pelvis')
    left   = bool(stance.get('left_arm'))
    lines  = ['## Stance (base-goal frame unless marked)']

    if not now:
        return lines + ['- Stance now: unknown']

    lines.append(f'- Stance now: forward {fmt(now["forward_m"])}, left {fmt(now["left_m"])}, yaw {fmt(now["yaw_rad"])}')
    button = to_base_goal(now, target) if target else None

    if target:
        lines += [f'- Target in pelvis: ahead {fmt(target[0])}, left {fmt(target[1])}, up {fmt(target[2])} '
                  f'({"left" if left else "right"} arm; worked range ahead '
                  f'{"0.37-0.42" if left else "0.45-0.52"})',
                  f'- Button in base-goal frame now: x {fmt(button[0])}, y {fmt(button[1])}']

    reference = stance.get('reference')
    jump      = None

    if reference and reference.get('target_in_pelvis'):
        then = to_base_goal(reference['stance_from_base_goal'], reference['target_in_pelvis'])
        lines.append(f'- Reference (last press of this button judged pressed, {"left" if reference.get("left_arm") else "right"} '
                     f'arm): stance forward {fmt(reference["stance_from_base_goal"]["forward_m"])}, left '
                     f'{fmt(reference["stance_from_base_goal"]["left_m"])}, yaw {fmt(reference["stance_from_base_goal"]["yaw_rad"])}; '
                     f'button then x {fmt(then[0])}, y {fmt(then[1])}')

        if button:
            jump = math.hypot(button[0] - then[0], button[1] - then[1])
            lines.append(f'- Button shift since the reference: {fmt(jump)} m')
    else:
        lines.append('- Reference: none for this button')

    if button:
        reachable = limits['reposition_m']['forward'] + REACH_AHEAD_MAX_M
        suspect   = (jump is not None and jump > DETECTION_JUMP_M) or button[0] > reachable
        lines.append(f'- Detection: {"SUSPECT" if suspect else "consistent"} (shift limit {DETECTION_JUMP_M} m, '
                     f'reachable x up to {reachable:.2f} m)')

    lines += candidate_lines(stance, now, button, left, phase, limits)

    if stance.get('other_arm'):
        other = stance['other_arm']
        lines.append(f'- Other arm: a {"left" if other.get("left_arm") else "right"}-arm press judged pressed, '
                     f'target {fmt(other.get("target_offset_m"))} m from this one')

    return lines


def candidate_lines(stance: dict, now: dict, button: list | None, left: bool, phase: str, limits: dict) -> list[str]:
    candidates = []

    if stance.get('match'):
        candidates.append(('Match (puts the target where the reference had it)', stance['match']))

    if button:
        ahead, side = NOMINAL_TARGET_M[left]
        candidates.append(('Nominal (facing the base heading, target ahead %.2f, left %.2f in pelvis)' % (ahead, side),
                           {'dx': button[0] - ahead, 'dy': button[1] - side, 'dyaw': 0.0}))

    if stance.get('reference') and phase == 'stance_check':
        ref = stance['reference']['stance_from_base_goal']
        candidates.append(('Reference stance', {'dx': ref['forward_m'], 'dy': ref['left_m'], 'dyaw': ref['yaw_rad']}))

    if phase == 'stance_check':
        candidates.append(('Back-off stance', {'dx': now['forward_m'] - BACK_OFF_M, 'dy': now['left_m'], 'dyaw': now['yaw_rad']}))

    candidates.append(('Base goal', {'dx': 0.0, 'dy': 0.0, 'dyaw': 0.0}))

    return [candidate_line(name, move, now, limits) for name, move in candidates]


def inside_limits(move: dict, limits: dict) -> bool:
    box = limits['reposition_m']

    return -box['back'] <= move['dx'] <= box['forward'] and abs(move['dy']) <= box['side']


def walk_text(away: float, limits: dict) -> str:
    """How the executor reaches a stance this far from the robot"""
    if away >= limits['walk_min_m']:
        return 'walks there'

    return f'walks out {limits["approach_back_m"]:g} m behind it, then onto it'


def candidate_line(name: str, move: dict, now: dict, limits: dict) -> str:
    away = math.hypot(move['dx'] - now['forward_m'], move['dy'] - now['left_m'])

    return (f'- {name}: dx {fmt(move["dx"])}, dy {fmt(move["dy"])}, dyaw {fmt(move["dyaw"])}; {fmt(away)} m from the robot '
            f'({walk_text(away, limits)}); {"inside" if inside_limits(move, limits) else "OUTSIDE"} the limits')


def reach_lines(reach: dict | None, move: dict | None, limits: dict) -> list[str]:
    if not reach:
        return ['## Reach check (IK before the press)', '- none since the last walk']

    moved = reach.get('target_moved_since_check_m')
    miss  = reach.get('pelvis_error_m') or {}
    lines = ['## Reach check (IK before the press)',
             f'- {"passed" if reach.get("reachable") else "failed"} ({"left" if reach.get("left_arm") else "right"} arm): '
             f'{reach.get("message", "")}',
             f'- Miss in pelvis, arm end minus target: x {fmt(miss.get("x"))}, y {fmt(miss.get("y"))}, z {fmt(miss.get("z"))} m',
             f'- Target moved since the check: {fmt(moved["norm"]) if moved else "unknown"} m (stance changed at '
             f'{limits["stance_changed_m"]:g} m or more)']

    if move:
        if move.get('fixable'):
            away = math.hypot(*move['step_pelvis_m']) if move.get('step_pelvis_m') else limits['walk_min_m']
            lines.append(f'- Reach move: dx {fmt(move.get("dx"))}, dy {fmt(move.get("dy"))}, dyaw {fmt(move.get("dyaw"))}; '
                         f'{fmt(away)} m from the robot ({walk_text(away, limits)}); '
                         f'{"inside" if move.get("within_limits") else "OUTSIDE"} the limits')
        else:
            lines.append(f'- Reach move: not fixable by walking: {move.get("why", "")}')

    return lines


def history_lines(history: list[dict]) -> list[str]:
    since = [record for record in history if record.get('skill') not in QUIET_SKILLS][-12:]
    tried = [record for record in history if record.get('skill') == 'manipulation_recovery' and record.get('decision')]
    lines = ['## This task so far (oldest first)']

    for record in since:
        if record.get('skill') == 'manipulation_recovery':
            lines.append(f'- recovery {record.get("phase")}: {record.get("decision")} {record.get("offset") or ""}'
                         f'{" (executor rule)" if str(record.get("rule")) == "True" else ""}')
        elif record.get('skill') == 'navigate_to':
            lines.append(f'- navigate_to {record.get("status")}: {record.get("message")}')
        elif record.get('skill') == 'set_press_offset':
            lines.append(f'- set_press_offset {record.get("inputs")}')
        elif record.get('decision'):
            lines.append(f'- {record.get("skill")}: {record.get("decision")}: {str(record.get("reason"))[:120]}')
        else:
            lines.append(f'- {record.get("skill")} {record.get("status")}: {str(record.get("message"))[:120]}')

    repositions = sum(record.get('decision') == 'reposition' for record in tried)
    retries     = sum(record.get('decision') == 'retry' for record in tried)

    return lines + [f'- Totals this task: {repositions} repositions, {retries} retries decided']


def to_base_goal(stance: dict, target: list) -> list:
    yaw = stance['yaw_rad']

    return [stance['forward_m'] + target[0] * math.cos(yaw) - target[1] * math.sin(yaw),
            stance['left_m'] + target[0] * math.sin(yaw) + target[1] * math.cos(yaw)]


def panel_type(button: str) -> str:
    if 'up' in button.split() or 'down' in button.split() or 'call' in button:
        return 'call panel, outside the car: the call button has its own light, rules 1-3'

    if 'floor' in button or any(char.isdigit() for char in button):
        return 'car panel: each button has its own light ring, rules 1-3'

    return 'unknown: decide from the frames whether the button has a ring'


def fmt(value) -> str:
    return f'{value:+.3f}' if isinstance(value, (int, float)) else str(value)


def vec(values) -> str:
    return '[' + ', '.join(fmt(value) for value in values) + ']'
