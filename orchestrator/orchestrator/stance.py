"""Where the robot stands for a press, against its base goal and against the last press on that button that worked.

A reference is kept for each press the judge called pressed: the robot's pose and the press target, in map and in pelvis.
For a later press at the same button, match() gives the reposition (relative to the base goal, as manipulation_recovery
states it) that puts the target where the reference had it in pelvis, and says whether navigate_to would walk it.
to_base_goal is the reposition back onto the base goal, which needs no reference. Without a reference for the button,
reach_match matches only the forward reach of the latest press the same arm made at any button, or either arm when the
same arm has none.
"""

import json
import math
import time

from pathlib import Path


ARRIVAL_RADIUS_M     = 0.1      # navigate_to reports arrived within this distance, so a smaller reposition does not walk
REFERENCE_DISTANCE_M = 1.0      # a reference counts for a target this close to its own, in map
DEFAULT_REFERENCES   = str(Path.home() / '.ros' / 'orchestrator_press_references.jsonl')


def relative_pose(robot: dict, base: dict) -> dict:
    """robot {x, y, yaw} in base's own frame: forward, left (m) and yaw (rad) from base"""
    yaw          = float(base.get('yaw', 0.0))
    dx, dy       = robot['x'] - float(base['x']), robot['y'] - float(base['y'])
    dyaw         = robot['yaw'] - yaw

    return {
        'forward_m': round(math.cos(yaw) * dx + math.sin(yaw) * dy, 4),
        'left_m':    round(-math.sin(yaw) * dx + math.cos(yaw) * dy, 4),
        'yaw_rad':   round(math.atan2(math.sin(dyaw), math.cos(dyaw)), 4),
    }


def transform_point(transform: dict, point) -> list:
    """point moved by an agent tf() result (translation_m, rotation_xyzw): source frame into target frame"""
    x, y, z, w = transform['rotation_xyzw']
    px, py, pz = point
    rotated    = [
        (1 - 2 * (y * y + z * z)) * px + 2 * (x * y - w * z) * py + 2 * (x * z + w * y) * pz,
        2 * (x * y + w * z) * px + (1 - 2 * (x * x + z * z)) * py + 2 * (y * z - w * x) * pz,
        2 * (x * z - w * y) * px + 2 * (y * z + w * x) * py + (1 - 2 * (x * x + y * y)) * pz,
    ]

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

    return {
        'dx':     round(stance['forward_m'] + math.cos(yaw) * step_forward - math.sin(yaw) * step_left, 3),
        'dy':     round(stance['left_m'] + math.sin(yaw) * step_forward + math.cos(yaw) * step_left, 3),
        'dyaw':   round(yaw, 3),
        'move_m': round(move_m, 3),
        'walks':  move_m > ARRIVAL_RADIUS_M,
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


def latest_for_arm(references: list[dict], left_arm: bool | None) -> dict | None:
    same = [reference for reference in references if left_arm is not None and bool(reference.get('left_arm')) == bool(left_arm)]

    return same[-1] if same else None


def load_references(path: str = DEFAULT_REFERENCES) -> list[dict]:
    file = Path(path).expanduser()

    if not file.is_file():
        return []

    return [json.loads(line) for line in file.read_text().splitlines() if line.strip()]


def save_reference(reference: dict, path: str = DEFAULT_REFERENCES):
    file = Path(path).expanduser()
    file.parent.mkdir(parents=True, exist_ok=True)

    with file.open('a') as out:
        out.write(json.dumps({'time': round(time.time(), 1), **reference}) + '\n')


def evidence(base: dict | None, robot: dict | None, target_pelvis: list | None, target_map: list | None, left_arm: bool | None,
             button: str, references: list[dict]) -> dict:
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
        'reach_match':           None,
    }

    if stance is not None:                      # the walk the tree planned, repeated: no reference needed
        move_m = math.hypot(stance['forward_m'], stance['left_m'])
        result['to_base_goal'] = {'dx': 0.0, 'dy': 0.0, 'dyaw': 0.0, 'move_m': round(move_m, 3), 'walks': move_m > ARRIVAL_RADIUS_M}

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

    reach = (latest_for_arm(references, left_arm) or (references[-1] if references else None)) if reference is None else None

    if stance is not None and target_pelvis is not None and reach is not None:   # the forward reach only: buttons differ sideways
        result['reach_match'] = {**match(stance, target_pelvis, [reach['target_pelvis'][0], target_pelvis[1]]),
                                 'reference_button': reach['button'], 'reference_left_arm': bool(reach['left_arm']),
                                 'reference_ahead_m': reach['target_pelvis'][0]}

    if reference is None:
        result['note'] = f'no press on "{button}" here has been judged pressed yet'
    elif result['match'] is None:
        result['note'] = 'no target yet: compare the stance with the reference stance'
    elif not result['match']['walks']:
        result['note'] = (f'the matching stance is {result["match"]["move_m"] * 100:.0f} cm away, inside the '
                          f'{ARRIVAL_RADIUS_M * 100:.0f} cm arrival radius: a reposition there does not walk')

    return result
