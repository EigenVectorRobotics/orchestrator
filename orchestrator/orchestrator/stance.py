"""Where the robot stands for a press, against its base goal and against the last press on that button that worked.

A reference is kept for each press the judge called pressed: the robot's pose and the press target, in map and in pelvis.
For a later press at the same button, match() gives the reposition (relative to the base goal, as manipulation_recovery
states it) that puts the target where the reference had it in pelvis, and says whether navigate_to would walk it.
to_base_goal is the reposition back onto the base goal, which needs no reference. reach is the last check_reach from this
stance, and reach_move the reposition its IK miss asks for, in the same base-goal frame.
"""

import json
import hashlib
import math
import time

from pathlib import Path

from .geometry import rotate, turn_2d, wrap
from .agent_spec import WALK_MIN_M       # navigate_to ends a shorter walk without a step


REACH_TOLERANCE_M    = 0.005    # ik_server's goal_position_tolerance: a smaller miss is an orientation miss
REACH_MAX_Z_M        = 0.03     # a larger vertical miss is not fixed by walking
REACH_MARGIN_M       = 0.02     # a reach move steps this much further, so the target lands inside the reach, not on its edge
REFERENCE_DISTANCE_M = 1.0      # a reference counts for a target this close to its own, in map
STATE_DIR            = Path.home() / 'orch_state'     # what the robot keeps across runs; run records are in ~/orch_runs
DEFAULT_REFERENCES   = str(STATE_DIR / 'press_references.jsonl')
OLD_REFERENCES       = Path.home() / '.ros' / 'orchestrator_press_references.jsonl'   # before 2026-11-17, still read


def relative_pose(robot: dict, base: dict) -> dict:
    """robot {x, y, yaw} in base's own frame: forward, left (m) and yaw (rad) from base"""
    yaw          = float(base.get('yaw', 0.0))
    dx, dy       = robot['x'] - float(base['x']), robot['y'] - float(base['y'])
    dyaw         = robot['yaw'] - yaw
    forward, left = turn_2d(-yaw, dx, dy)

    return {
        'forward_m': round(forward, 4),
        'left_m':    round(left, 4),
        'yaw_rad':   round(wrap(dyaw), 4),
    }


def transform_point(transform: dict, point) -> list:
    """point moved by an agent tf() result (translation_m, rotation_xyzw): source frame into target frame"""
    rotated = rotate(tuple(transform['rotation_xyzw']), tuple(point))

    return [round(value + offset, 4) for value, offset in zip(rotated, transform['translation_m'])]


def pose_2d(transform: dict) -> dict:
    """An agent tf() result as {x, y, yaw}"""
    return {'x': transform['translation_m'][0], 'y': transform['translation_m'][1], 'yaw': transform['rpy_rad'][2]}


def last_goal(failure: dict | None) -> list | None:
    """The last press goal of a failed press, or of the press a failed check checked, as [x, y, z] in its frame"""
    call = (failure or {}).get('checked_call') or failure or {}
    goals = [sample.get('current_goal') for sample in call.get('feedback') or [] if isinstance(sample, dict) and sample.get('current_goal')]

    if not goals:
        return None

    position = (goals[-1].get('pose') or {}).get('position') or goals[-1]

    return [float(position['x']), float(position['y']), float(position['z'])]


def match(stance: dict, target_pelvis: list, reference_pelvis: list) -> dict:
    """The stance, relative to the base goal, that puts the target at reference_pelvis in pelvis, keeping the heading.
    The robot steps by the target's offset from the reference, in its own frame"""
    step_forward = target_pelvis[0] - reference_pelvis[0]
    step_left    = target_pelvis[1] - reference_pelvis[1]
    yaw          = stance['yaw_rad']
    move_m       = math.hypot(step_forward, step_left)
    step_x, step_y = turn_2d(yaw, step_forward, step_left)

    return {
        'dx':     round(stance['forward_m'] + step_x, 3),
        'dy':     round(stance['left_m'] + step_y, 3),
        'dyaw':   round(yaw, 3),
        'move_m': round(move_m, 3),
        'walks':  move_m > WALK_MIN_M,
    }


def nearest_reference(references: list[dict], button: str, target_map: list | None, base: dict | None,
                      left_arm: bool | None = None) -> dict | None:
    """The latest reference for this button whose target lies within REFERENCE_DISTANCE_M of the target (or of the
    base goal when there is no target yet)"""
    if target_map is not None:
        anchor = target_map[:2]
    elif base is not None:
        anchor = [float(base['x']), float(base['y'])]
    else:
        return None

    close = [reference for reference in references
             if reference.get('button') == button and math.dist(reference['target_map'][:2], anchor) < REFERENCE_DISTANCE_M
             and (left_arm is None or bool(reference.get('left_arm')) == bool(left_arm))]

    return close[-1] if close else None


def _references_file(path: str) -> Path:
    """The references file; the old one under ~/.ros while the default one does not exist yet"""
    file = Path(path).expanduser()

    return OLD_REFERENCES if path == DEFAULT_REFERENCES and not file.is_file() and OLD_REFERENCES.is_file() else file


def load_references(path: str = DEFAULT_REFERENCES) -> list[dict]:
    file = _references_file(path)

    if not file.is_file():
        return []

    return [json.loads(line) for line in file.read_text().splitlines() if line.strip()]


def save_reference(reference: dict, path: str = DEFAULT_REFERENCES):
    file = Path(path).expanduser()
    file.parent.mkdir(parents=True, exist_ok=True)

    if not file.exists() and _references_file(path) != file:   # the first save moves the old references over
        file.write_bytes(OLD_REFERENCES.read_bytes())

    with file.open('a') as out:
        out.write(json.dumps({'time': round(time.time(), 1), **reference}) + '\n')


def references_state(path: str = DEFAULT_REFERENCES) -> dict:
    """Which references file is read and what it holds, for a run's provenance"""
    file = _references_file(path)

    if not file.is_file():
        return {'path': str(file), 'lines': 0, 'sha256': None}

    data = file.read_bytes()

    return {'path': str(file), 'lines': data.count(b'\n'), 'sha256': hashlib.sha256(data).hexdigest()[:16]}


def latest_reach(failure: dict | None, history: list[dict] | None) -> dict | None:
    """The last check_reach answer since the last walk: the miss in the pelvis frame, on its three axes, and the target there.
    The history holds the failure too; the failure alone stands in for an empty history"""
    for record in list(reversed(history or [])) or [failure or {}]:
        if record.get('skill') == 'navigate_to':      # the robot walked after it: that answer is for another stance
            return None

        if record.get('skill') != 'check_reach':
            continue

        outputs = record.get('outputs') or {}

        return {
            'reachable':             record.get('status') == 'success',
            'left_arm':              bool((record.get('inputs') or {}).get('left_arm', False)),
            'message':               record.get('message', ''),
            'position_error_m':      outputs.get('position_error'),
            'orientation_error_deg': outputs.get('orientation_error_deg'),
            'pelvis_error_m':        _axes(outputs, 'pelvis_error'),
            'target_in_pelvis':      _axes(outputs, 'target_pelvis'),
            'frame':                 'pelvis: x forward, y left, z up; arm end minus target, so x < 0 means the arm stops short',
        }

    return None


def _axes(outputs: dict, name: str) -> dict | None:
    return {axis: outputs[f'{name}_{axis}'] for axis in 'xyz'} if f'{name}_x' in outputs else None


def reach_move(stance: dict | None, reach: dict | None, limits: dict | None = None) -> dict | None:
    """The reposition, relative to the base goal, that cancels the IK miss's horizontal part, keeping the heading.
    The robot steps by minus the miss, plus REACH_MARGIN_M, in its own (pelvis) frame. None without a failed check with a miss"""
    miss = (reach or {}).get('pelvis_error_m')

    if stance is None or miss is None or reach.get('reachable'):
        return None

    horizontal = math.hypot(miss['x'], miss['y'])

    if horizontal <= REACH_TOLERANCE_M or abs(miss['z']) > REACH_MAX_Z_M:
        return {'fixable': False, 'why': f'the miss is {horizontal * 1000:.0f} mm across and {miss["z"] * 1000:+.0f} mm up: '
                                          'walking fixes only a horizontal miss, with at most '
                                          f'{REACH_MAX_Z_M * 1000:.0f} mm up or down'}

    scale        = (horizontal + REACH_MARGIN_M) / horizontal
    step_forward = -miss['x'] * scale
    step_left    = -miss['y'] * scale
    found        = match(stance, [step_forward, step_left], [0.0, 0.0])
    found.update({'fixable': True, 'step_pelvis_m': [round(step_forward, 3), round(step_left, 3)]})

    if limits is not None:
        found['within_limits'] = -limits['back'] <= found['dx'] <= limits['forward'] and abs(found['dy']) <= limits['side']

    return found


def evidence(base: dict | None, robot: dict | None, target_pelvis: list | None, target_map: list | None, left_arm: bool | None,
             button: str, references: list[dict], reach: dict | None = None, limits: dict | None = None) -> dict:
    """What manipulation_recovery gets as get_stance"""
    stance    = relative_pose(robot, base) if robot is not None and base is not None else None
    reference = nearest_reference(references, button, target_map, base, left_arm)
    other     = nearest_reference(references, button, target_map, base, None if left_arm is None else not left_arm)
    result    = {
        'stance_from_base_goal': stance,
        'target_in_pelvis':      target_pelvis,
        'left_arm':              left_arm,
        'reference':             None,
        'match':                 None,
        'other_arm':             None,
        'to_base_goal':          None,
        'reach':                 reach,
        'reach_move':            reach_move(stance, reach, limits),
    }

    if reach is not None and reach.get('target_in_pelvis') and target_pelvis is not None:   # the press goal against the checked one
        moved = [round(target_pelvis[index] - reach['target_in_pelvis'][axis], 3) for index, axis in enumerate('xyz')]
        reach['target_moved_since_check_m'] = {'xyz': moved, 'norm': round(math.hypot(*moved), 3)}

    if stance is not None:                      # the walk the tree planned, repeated: no reference needed
        move_m = math.hypot(stance['forward_m'], stance['left_m'])
        result['to_base_goal'] = {'dx': 0.0, 'dy': 0.0, 'dyaw': 0.0, 'move_m': round(move_m, 3), 'walks': move_m > WALK_MIN_M}

    if other is not None and left_arm is not None:
        result['other_arm'] = {
            'when':             other['time'],
            'left_arm':         other['left_arm'],
            'target_in_pelvis': other['target_pelvis'],
            'target_offset_m':  round(math.dist(other['target_pelvis'][:2], target_pelvis[:2]), 3) if target_pelvis else None,
        }

    if reference is not None:
        result['reference'] = {
            'when':             reference['time'],
            'left_arm':         reference['left_arm'],
            'target_in_pelvis': reference['target_pelvis'],
            'stance_from_base_goal': relative_pose(reference['robot'], base) if base is not None else None,
        }

    if stance is not None and target_pelvis is not None and reference is not None:
        result['match'] = match(stance, target_pelvis, reference['target_pelvis'])

    if reference is None:
        result['note'] = f'no press on "{button}" here has been judged pressed yet'
    elif result['match'] is None:
        result['note'] = 'no target yet: compare the stance with the reference stance'
    elif not result['match']['walks']:
        result['note'] = (f'the matching stance is {result["match"]["move_m"] * 100:.0f} cm away, inside the '
                          f'{WALK_MIN_M * 100:.0f} cm arrival radius: a reposition there does not walk')

    return result
