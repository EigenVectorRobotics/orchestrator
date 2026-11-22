import math

from ..lib.press import since_last_pressed
from ..lib.walk import offset_goal
from .spec import STANCE_CHANGED_M, STANCE_TOLERANCE_M, STANCE_TOLERANCE_RAD


# The executor's own rules for a plain stance; a missed press always goes to the model, with its aim crops
STANCE_RULE_MAX     = 2                 # stances re-sent per press before the agent decides
REACH_RULE_MAX      = 2                 # reach moves per press before the agent decides


def _rule_records(history: list[dict], rule_name: str) -> list[dict]:
    return [record for record in since_last_pressed(history) if record.get('skill') == 'manipulation_recovery'
            and record.get('rule') and record.get('rule_name') == rule_name]


def rule_stance(evidence: dict | None, history: list[dict]) -> dict | None:
    """reposition onto the target stance without the agent, when the robot stands off it by more than the stance tolerance.
    The target is the offset of the last reposition since the last press judged pressed, else the base goal. A continue
    after that reposition sets the tree's goal to the base goal again, so the target is then the base goal too. At most
    STANCE_RULE_MAX times; then the agent decides"""
    off   = (evidence or {}).get('stance_from_base_goal')
    moved = [record.get('offset') if record.get('decision') == 'reposition' else None for record in since_last_pressed(history)
             if record.get('skill') == 'manipulation_recovery' and (record.get('decision') == 'continue'
             or (record.get('decision') == 'reposition' and isinstance(record.get('offset'), dict)))]

    if off is None or len(_rule_records(history, 'stance')) >= STANCE_RULE_MAX:
        return None

    # The agent may have asked to stand off the base goal on purpose (a back-off for the camera): that stance is the target
    target   = {key: float((moved[-1] if moved and moved[-1] else {}).get(key, 0.0)) for key in ('dx', 'dy', 'dyaw')}
    forward  = off['forward_m'] - target['dx']
    left     = off['left_m'] - target['dy']
    yaw      = math.atan2(math.sin(off['yaw_rad'] - target['dyaw']), math.cos(off['yaw_rad'] - target['dyaw']))
    distance = math.hypot(forward, left)

    if distance <= STANCE_TOLERANCE_M and abs(yaw) <= STANCE_TOLERANCE_RAD:
        return None

    goal = 'the base goal' if not moved else f'the target stance (dx {target["dx"]:+.3f}, dy {target["dy"]:+.3f})'

    return {
        'decision':  'reposition',
        'rule_name': 'stance',
        'reason':   f'rule: the robot stands {distance:.3f} m and {math.degrees(yaw):+.1f} deg off {goal} '
                    f'(forward {forward:+.3f}, left {left:+.3f}): sending it again',
        **target,
    }


def rule_reach(evidence: dict | None, history: list[dict], last_failure: dict | None) -> dict | None:
    """reposition by the IK miss without the agent: a failed check_reach at this stance whose miss walking can fix
    within the reposition limits. At most REACH_RULE_MAX times since the last press judged pressed; then the agent decides"""
    move  = (evidence or {}).get('reach_move') or {}
    reach = (evidence or {}).get('reach') or {}

    if (last_failure or {}).get('skill') != 'check_reach' or not move.get('fixable') or not move.get('within_limits'):
        return None

    if len(_rule_records(history, 'reach')) >= REACH_RULE_MAX:
        return None

    miss = reach['pelvis_error_m']

    return {
        'decision':  'reposition',
        'rule_name': 'reach',
        'reason':   f'rule: the IK check missed by x {miss["x"] * 1000:+.0f} y {miss["y"] * 1000:+.0f} z {miss["z"] * 1000:+.0f} mm '
                    f'in pelvis (arm end minus target): stepping {move["step_pelvis_m"]} m in pelvis, to dx {move["dx"]:+.3f} '
                    f'dy {move["dy"]:+.3f} dyaw {move["dyaw"]:+.3f} from the base goal',
        'dx': move['dx'], 'dy': move['dy'], 'dyaw': move['dyaw'],
    }


def stance_unchanged(evidence: dict | None) -> str:
    """Why a diagnosis may not reposition: the press ran from a stance check_reach passed, and its target has not moved
    in pelvis since. Empty when a reposition is open"""
    reach = (evidence or {}).get('reach') or {}
    moved = reach.get('target_moved_since_check_m')

    if not reach.get('reachable') or moved is None or moved['norm'] >= STANCE_CHANGED_M:
        return ''

    return (f'check_reach passed from this stance ({reach.get("message", "")}), and the press target has moved only '
            f'{moved["norm"] * 1000:.0f} mm in pelvis since (under {STANCE_CHANGED_M * 1000:.0f} mm): the stance did not '
            'change, so walking does not fix this press. Look at tracking, the target, aim or a step instead')


def decision_outputs(decision: dict, inputs: dict, last_failure: dict | None) -> dict:
    """What the tree gets: decision, reason, goal (base_goal, moved for a reposition), left_arm, offset. Whether a walk
    to goal needs a walk-out first is decided before each look, from where the robot stands then (near_pose)"""
    failed_call   = (last_failure or {}).get('checked_call') or last_failure or {}     # a failed check: the call it checked
    failed_inputs = failed_call.get('inputs', {})
    base_goal     = inputs.get('base_goal')
    outputs       = {
        'decision': decision['decision'],
        'reason':   decision['reason'],
        'left_arm': bool(failed_inputs.get('left_arm', False)),
    }

    if base_goal is not None:
        outputs['goal'] = base_goal

    if decision['decision'] == 'reposition':
        offset              = {key: float(decision.get(key) or 0.0) for key in ('dx', 'dy', 'dyaw')}
        outputs['offset']   = offset
        outputs['goal']     = offset_goal(base_goal, offset['dx'], offset['dy'], offset['dyaw'])

    if decision['decision'] == 'switch_arm':
        outputs['left_arm'] = bool(decision['use_left_arm'])

    if decision['decision'] == 'offset_retry':
        outputs.update({f'offset_{axis}': float(decision[f'o{axis}']) for axis in 'xyz'})

    return outputs
