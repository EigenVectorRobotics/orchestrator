"""Agent skills: one decision of a tree, handed to a model or to the operator through the agent bridge.

The tree calls an agent skill like any other leaf (AgentLeaf in skill_leaf). The executor gathers the evidence once
(the failure the tree recorded, the history, the press frames, the stance, the camera image), the backend sends it
with the task, and one decision comes back. The executor checks it, and the tree acts on it.
"""

import json
import math
import threading

from typing import Callable, Protocol
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

from skill_registry import Contract

from .behavior_tree import Context, Status
from . import stance
from .geometry import turn_2d, wrap
from .agent_spec import (APPROACH_BACK_M, GOING_ON, MAX_PRESS_OFFSET_M, PHASE_DECISIONS, REPOSITION_LIMITS_M,
                         STANCE_CHANGED_M, STANCE_PHASES, STANCE_TOLERANCE_M, STANCE_TOLERANCE_RAD, WALK_MIN_M)


# The phases, decisions and limits the bridge shares are in agent_spec; these are the executor's own rules
RULE_MISS_M         = (0.005, 0.050)    # a first miss this far from the goal in the button plane is corrected without the agent
RULE_SHORT_M        = 0.003             # a press that stopped this far short along the button normal, aimed well, goes deeper
RULE_DEPTH_MARGIN_M = 0.005             # pushed this much past the shortfall
STANCE_RULE_MAX     = 2                 # stances re-sent per press before the agent decides
REACH_RULE_MAX      = 2                 # reach moves per press before the agent decides
PRESS_FRAME   = 'foot_world'        # press_button's goal frame (move_to_pose planning_frame)

# What the robot says when an agent call opens and when it is decided, so people near it know what comes next
PHASE_SPEECH    = {
    'diagnose':     'The press did not work. Checking why.',
    'stance_check': 'I cannot see the button well from here. Checking where I stand.',
    'judge':        'Checking whether the button was pressed.',
}
DECISION_SPEECH = {
    'reposition':   'Moving to a better spot.',
    'switch_arm':   'Switching to my other arm.',
    'offset_retry': 'Correcting my aim and pressing again.',
    'retry':        'Trying the press again.',
    'abort':        'I cannot press this button. Stopping the task.',
    'continue':     'My stance looks fine. Looking at the button again from here.',
    'pressed':      'The button is pressed.',
    'not_pressed':  'I missed the button.',
}


class AgentError(RuntimeError):
    pass


@dataclass
class ToolOutput:
    text:       str
    image:      bytes | None = None
    media_type: str = 'image/jpeg'
    is_error:   bool = False
    final:      bool = False        # decide accepted the decision: the call is over


class RosAccess(Protocol):
    """Read-only access to the robot, from the executor's node"""

    def tf(self, target_frame: str, source_frame: str, timeout_s: float) -> dict: ...
    def image(self, source: str) -> tuple[bytes, str]: ...


class AgentBackend(Protocol):
    name: str

    def run(
        self,
        skill: str,
        phase: str,
        task: str,
        session: 'AgentSession',
        should_stop: Callable[[], bool],
        on_event: Callable[..., None]
    ): ...



def offset_goal(base: dict, dx: float, dy: float, dyaw: float) -> dict:
    """base moved by (dx, dy) in its own frame (x along its yaw) and turned by dyaw"""
    yaw          = float(base.get('yaw', 0.0))
    step_x, step_y = turn_2d(yaw, dx, dy)

    goal = {
        'x':     float(base['x']) + step_x,
        'y':     float(base['y']) + step_y,
        'yaw':   wrap(yaw + dyaw),
        'frame': base.get('frame', 'map'),
    }

    if 'z' in base:                     # the same storey as the base goal
        goal['z'] = float(base['z'])

    return goal


def offset_in_force(history: list[dict], press_offset: list | None = None) -> list[float]:
    """The press offset (x in, y left, z up) the last press used: press_offset from its capture, else the last
    set_press_offset since the last press judged pressed (0 when there is none)"""
    if press_offset is not None and len(press_offset) == 3:
        return [float(value) for value in press_offset]

    pressed = [index for index, record in enumerate(history) if record.get('skill') == 'press_judge' and record.get('decision') == 'pressed']
    since   = history[pressed[-1] + 1:] if pressed else history
    offsets = [record.get('inputs') or {} for record in since if record.get('skill') == 'set_press_offset']
    last    = offsets[-1] if offsets else {}

    return [float(last.get(axis, 0.0)) for axis in 'xyz']


def rule_offset(last_failure: dict | None, history: list[dict], residual: list | None,
                press_offset: list | None = None) -> dict | None:
    """offset_retry without the agent: the first diagnosis after a press that reached its goal and missed the button
    by RULE_MISS_M in its plane, or hit it in the plane but stopped over RULE_SHORT_M short along its normal. The new
    offset is the offset in force minus the residual, as the agent would set it, and RULE_DEPTH_MARGIN_M deeper if short.
    press_offset: the offset the press used, from its capture; without it, the last set_press_offset since the last
    press judged pressed (0 when there is none), which misses an offset set before that press"""
    failure = last_failure or {}
    pressed = [index for index, record in enumerate(history) if record.get('skill') == 'press_judge' and record.get('decision') == 'pressed']
    since   = history[pressed[-1] + 1:] if pressed else history

    if failure.get('skill') != 'press_judge' or not failure.get('checked_call') or residual is None or len(residual) != 3:
        return None

    if any(record.get('skill') == 'manipulation_recovery' and record.get('phase') == 'diagnose' for record in since):
        return None

    miss  = math.hypot(residual[1], residual[2])
    short = residual[0] < -RULE_SHORT_M

    if not (RULE_MISS_M[0] < miss < RULE_MISS_M[1] or (miss <= RULE_MISS_M[0] and short)):
        return None

    in_force  = offset_in_force(history, press_offset)
    corrected = [round(force - miss_axis, 4) for force, miss_axis in zip(in_force, residual)]
    corrected[0] = round(corrected[0] + (RULE_DEPTH_MARGIN_M if short else 0.0), 4)

    if max(abs(value) for value in corrected) > MAX_PRESS_OFFSET_M:
        return None

    found = f'missed by {miss * 1000:.1f} mm in the button plane' if miss > RULE_MISS_M[0] else 'hit the button in its plane'
    depth = f' and stopped {-residual[0] * 1000:.1f} mm short (pushing {RULE_DEPTH_MARGIN_M * 1000:g} mm past it)' if short else ''

    return {
        'decision':  'offset_retry',
        'rule_name': 'offset',
        'reason':   f'rule: the press reached its goal, {found}{depth} '
                    f'(residual {[round(value * 1000, 1) for value in residual]} mm); offset {[round(v * 1000, 1) for v in in_force]} '
                    f'-> {[round(v * 1000, 1) for v in corrected]} mm',
        'ox': corrected[0], 'oy': corrected[1], 'oz': corrected[2],
    }


def rule_stance(evidence: dict | None, history: list[dict]) -> dict | None:
    """reposition onto the target stance without the agent, when the robot stands off it by more than the stance tolerance.
    The target is the offset of the last reposition since the last press judged pressed, else the base goal. A continue
    after that reposition sets the tree's goal to the base goal again, so the target is then the base goal too. At most
    STANCE_RULE_MAX times; then the agent decides"""
    off     = (evidence or {}).get('stance_from_base_goal')
    pressed = [index for index, record in enumerate(history) if record.get('skill') == 'press_judge' and record.get('decision') == 'pressed']
    since   = history[pressed[-1] + 1:] if pressed else history
    sent    = [record for record in since if record.get('skill') == 'manipulation_recovery' and record.get('rule')
               and record.get('rule_name') == 'stance']
    moved   = [record.get('offset') if record.get('decision') == 'reposition' else None for record in since
               if record.get('skill') == 'manipulation_recovery' and (record.get('decision') == 'continue'
               or (record.get('decision') == 'reposition' and isinstance(record.get('offset'), dict)))]

    if off is None or len(sent) >= STANCE_RULE_MAX:
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
    within REPOSITION_LIMITS_M. At most REACH_RULE_MAX times since the last press judged pressed; then the agent decides"""
    move    = (evidence or {}).get('reach_move') or {}
    reach   = (evidence or {}).get('reach') or {}
    pressed = [index for index, record in enumerate(history) if record.get('skill') == 'press_judge' and record.get('decision') == 'pressed']
    since   = history[pressed[-1] + 1:] if pressed else history
    sent    = [record for record in since if record.get('skill') == 'manipulation_recovery' and record.get('rule')
               and record.get('rule_name') == 'reach']

    if (last_failure or {}).get('skill') != 'check_reach' or not move.get('fixable') or not move.get('within_limits'):
        return None

    if len(sent) >= REACH_RULE_MAX:
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


def decision_outputs(decision: dict, inputs: dict, last_failure: dict | None, robot: dict | None = None) -> dict:
    """What the tree gets: decision, reason, goal (base_goal, moved for a reposition), approach, walk_in, left_arm, offset"""
    failed_call   = (last_failure or {}).get('checked_call') or last_failure or {}     # a failed check: the call it checked
    failed_inputs = failed_call.get('inputs', {})
    base_goal     = inputs.get('base_goal')
    outputs       = {
        'decision': decision['decision'],
        'reason':   decision['reason'],
        'left_arm': bool(failed_inputs.get('left_arm', False)),
        'walk_in':  'no',
    }

    if base_goal is not None:
        outputs['goal'] = base_goal

    if decision['decision'] == 'reposition':
        offset            = {key: float(decision.get(key, 0.0)) for key in ('dx', 'dy', 'dyaw')}
        outputs['offset'] = offset
        outputs['goal']   = offset_goal(base_goal, offset['dx'], offset['dy'], offset['dyaw'])
        outputs['approach'] = outputs['goal']

        if robot is not None and math.hypot(outputs['goal']['x'] - robot['x'], outputs['goal']['y'] - robot['y']) < WALK_MIN_M:
            outputs['walk_in']  = 'yes'
            outputs['approach'] = offset_goal(outputs['goal'], -APPROACH_BACK_M, 0.0, 0.0)

    if decision['decision'] == 'switch_arm':
        outputs['left_arm'] = bool(decision['use_left_arm'])

    if decision['decision'] == 'offset_retry':
        outputs.update({f'offset_{axis}': float(decision[f'o{axis}']) for axis in 'xyz'})

    return outputs



class AgentSession:
    """One agent call: the evidence gathered for it, the camera, and the decision once the executor accepts it.
    The backend reads the evidence as it is and sends each decision it gets through decide()"""

    def __init__(self, phase: str, inputs: dict, evidence: dict, ros: RosAccess | None, on_event):
        self.phase    = phase
        self.inputs   = inputs
        self.evidence = evidence
        self.ros      = ros
        self.on_event = on_event
        self.decision = None

    def camera(self) -> tuple[bytes, str] | None:
        """The latest camera frame and its media type, or None when there is none"""
        if self.ros is None:
            return None

        try:
            return self.ros.image('camera')
        except Exception as error:                      # no frame yet: the call goes without the image
            self.on_event('agent_camera', f'no camera image: {error}', level='warn')
            return None

    def decide(self, arguments: dict) -> ToolOutput:
        """Check a decision; final when accepted. A refusal says why, for the backend to ask again"""
        try:
            output = self._tool_decide(**arguments)
        except TypeError as error:
            output = ToolOutput(f'bad decision fields: {error}', is_error=True)

        if output.is_error:
            self.on_event('agent_refused', output.text[:300], level='warn')

        return output

    def _tool_decide(self, decision: str, reason: str, dx: float | None = None, dy: float | None = None,
                     dyaw: float | None = None, use_left_arm: bool | None = None, ox: float | None = None,
                     oy: float | None = None, oz: float | None = None) -> ToolOutput:
        if self.decision is not None:
            return ToolOutput('you have already decided', is_error=True)

        allowed = PHASE_DECISIONS[self.phase]

        if decision not in allowed:
            return ToolOutput(f'decision must be one of {list(allowed)} in phase {self.phase}', is_error=True)

        if not reason.strip():
            return ToolOutput('give a reason', is_error=True)

        if decision == 'reposition':
            if self.inputs.get('base_goal') is None:
                return ToolOutput('this call has no base_goal, so a reposition cannot be carried out; choose another decision', is_error=True)

            if dx is None or dy is None:
                return ToolOutput('reposition needs dx and dy (dyaw defaults to 0)', is_error=True)

            if self.phase == 'diagnose' and (unchanged := stance_unchanged(self.evidence.get('stance'))):
                return ToolOutput(unchanged, is_error=True)

            if dx > REPOSITION_LIMITS_M['forward'] or dx < -REPOSITION_LIMITS_M['back'] or abs(dy) > REPOSITION_LIMITS_M['side']:
                return ToolOutput(f'a reposition stays within {REPOSITION_LIMITS_M["forward"]:g} m past the base goal, '
                                  f'{REPOSITION_LIMITS_M["back"]:g} m behind it and {REPOSITION_LIMITS_M["side"]:g} m to the side: '
                                  'the base goal stands close to the panel, so more puts the robot into it. A target that '
                                  'looks farther than that is a wrong detection, not a wrong stance: choose another decision',
                                  is_error=True)

        if decision == 'switch_arm' and use_left_arm is None:
            return ToolOutput('switch_arm needs use_left_arm', is_error=True)

        if decision == 'offset_retry':
            if ox is None or oy is None or oz is None:
                return ToolOutput('offset_retry needs ox, oy and oz: the whole press offset to use, in metres', is_error=True)

            if max(abs(ox), abs(oy), abs(oz)) > MAX_PRESS_OFFSET_M:
                return ToolOutput(f'offset_retry corrects at most {MAX_PRESS_OFFSET_M * 1000:g} mm per axis; a larger '
                                  'miss is a wrong target or a wrong stance: choose another decision', is_error=True)

        self.decision = {
            'decision':     decision,
            'reason':       reason.strip(),
            'dx':           dx or 0.0,
            'dy':           dy or 0.0,
            'dyaw':         dyaw or 0.0,
            'use_left_arm': use_left_arm,
            'ox':           ox,
            'oy':           oy,
            'oz':           oz,
        }
        self.on_event('agent_decision', f'{decision}: {reason.strip()}', decision=self.decision)

        return ToolOutput(f'decision recorded: {decision}', final=True)


# ========== Running a call ==========

class AgentHandle:
    """One running agent call, polled by the leaf. Cancelling tells the backend to drop it"""

    def __init__(self, pool: ThreadPoolExecutor, work: Callable[[threading.Event], dict]):
        self.status    = Status.RUNNING
        self.message   = ''
        self.outputs   = {}
        self._stop     = threading.Event()
        self._future   = pool.submit(work, self._stop)

    def poll(self) -> Status:
        if self.status != Status.RUNNING or not self._future.done():
            return self.status

        try:
            self.outputs = self._future.result()
        except Exception as error:                      # the backend, the network, or no decision
            self.status, self.message = Status.FAILURE, f'agent failed: {error}'
            return self.status

        decision     = self.outputs['decision']
        self.status  = Status.SUCCESS if decision in GOING_ON else Status.FAILURE
        self.message = f'{decision}: {self.outputs["reason"]}'

        return self.status

    def cancel(self):
        self._stop.set()


class Agent:
    """Runs agent skills for the executor: one call at a time, each in a thread"""

    def __init__(self, backend: AgentBackend, ros: RosAccess | None = None, on_event=None,
                 speak: Callable[[str], None] | None = None, press_evidence: Callable[[], dict | None] | None = None):
        self.press_evidence = press_evidence    # the last press's saved frames and numbers, for the judge phase
        self.backend  = backend
        self.ros      = ros
        self.speak    = speak
        self.on_event = on_event or (lambda kind, text, **data: None)
        self._pool    = ThreadPoolExecutor(max_workers=1)

    def start(self, contract: Contract, inputs: dict, context: Context) -> AgentHandle:
        phase    = inputs.get('phase', 'diagnose')
        evidence = json.loads(json.dumps({'last_failure': context.last_failure, 'history': context.history}, default=str))

        def work(stop: threading.Event) -> dict:
            # (1) The evidence, gathered once
            if phase == 'judge':                # waits until the press's frames are chosen, then saves them
                evidence['press'] = self.press_evidence() if self.press_evidence else None

            if phase == 'diagnose' and (evidence['last_failure'] or {}).get('skill') == 'press_judge' and self.press_evidence:
                evidence['press'] = self.press_evidence()      # the judged press: its frames are chosen already

            if phase in STANCE_PHASES:
                try:
                    evidence['stance'] = self._stance(inputs, evidence)
                except Exception as error:              # a missing transform leaves the call without a stance, not failed
                    evidence['stance'] = {'error': str(error)}

            # (2) A rule decides without the agent when the evidence makes the answer plain
            if phase == 'diagnose' and (rule := self._rule(evidence)) is not None:
                self.on_event('agent_rule', rule['reason'], phase=phase)
                return {**decision_outputs(rule, inputs, evidence['last_failure'], self._robot()), 'rule': True, 'rule_name': rule['rule_name']}

            if phase in STANCE_PHASES and (rule := rule_stance(evidence.get('stance'), evidence['history'])) is not None:
                self.on_event('agent_rule', rule['reason'], phase=phase)
                self._say('I am not standing where I should. Walking to my spot again.')
                return {**decision_outputs(rule, inputs, evidence['last_failure'], self._robot()), 'rule': True, 'rule_name': rule['rule_name']}

            if phase == 'stance_check' and (rule := rule_reach(evidence.get('stance'), evidence['history'], evidence['last_failure'])):
                self.on_event('agent_rule', rule['reason'], phase=phase)
                self._say('The button is just out of my reach. Stepping closer to it.')
                return {**decision_outputs(rule, inputs, evidence['last_failure'], self._robot()), 'rule': True, 'rule_name': rule['rule_name']}

            # (3) The call: the backend sends the evidence and brings back one decision that decide accepted
            session = AgentSession(phase, inputs, evidence, self.ros, self.on_event)

            self.on_event('agent_start', f'{contract.name} ({phase}) on {self.backend.name}', phase=phase)
            self._say(PHASE_SPEECH.get(phase))
            self.backend.run(contract.interface.skill, phase, _task(phase, inputs), session, stop.is_set, self.on_event)

            if session.decision is None:
                raise AgentError('the agent ended without a decision')

            self._say(DECISION_SPEECH.get(session.decision['decision']))

            if phase == 'judge' and session.decision['decision'] == 'pressed':
                self._keep_reference(inputs, evidence.get('press') or {})

            return decision_outputs(session.decision, inputs, evidence['last_failure'], self._robot())

        return AgentHandle(self._pool, work)

    def _rule(self, evidence: dict) -> dict | None:
        failure = evidence.get('last_failure') or {}

        if failure.get('skill') != 'press_judge' or not self.press_evidence:
            return None

        press = self.press_evidence() or {}
        rule  = rule_offset(failure, evidence.get('history') or [], (press.get('capture') or {}).get('residual_m'),
                            press.get('press_offset_m'))

        if rule is not None:
            self._say('The press just missed the button. Correcting my aim and pressing again.')

        return rule

    def _stance(self, inputs: dict, evidence: dict) -> dict | None:
        """get_stance: the robot against the base goal and against the last press on this button that worked"""
        if self.ros is None:
            return None

        failure = evidence.get('last_failure') or {}
        goal    = stance.last_goal(failure)
        call    = failure.get('checked_call') or failure
        robot   = stance.pose_2d(self.ros.tf('map', 'pelvis', 1.0))
        button  = inputs.get('target_description', '')

        return stance.evidence(inputs.get('base_goal'), robot,
                               stance.transform_point(self.ros.tf('pelvis', PRESS_FRAME, 1.0), goal) if goal else None,
                               stance.transform_point(self.ros.tf('map', PRESS_FRAME, 1.0), goal) if goal else None,
                               (call.get('inputs') or {}).get('left_arm'), button, stance.load_references(),
                               stance.latest_reach(failure, evidence.get('history')), REPOSITION_LIMITS_M)

    def _robot(self) -> dict | None:
        if self.ros is None:
            return None

        try:
            return stance.pose_2d(self.ros.tf('map', 'pelvis', 1.0))
        except Exception:                               # no transform: the tree walks straight to the goal
            return None

    def _keep_reference(self, inputs: dict, press: dict):
        """A press judged pressed becomes the reference for later presses on this button"""
        if self.ros is None or not press.get('goal'):
            return

        try:
            stance.save_reference({
                'button':        inputs.get('target_description', ''),
                'left_arm':      bool(press.get('left_arm')),
                'robot':         stance.pose_2d(self.ros.tf('map', 'pelvis', 1.0)),
                'target_pelvis': stance.transform_point(self.ros.tf('pelvis', PRESS_FRAME, 1.0), press['goal']),
                'target_map':    stance.transform_point(self.ros.tf('map', PRESS_FRAME, 1.0), press['goal']),
            })
        except Exception as error:                      # a missing transform costs the reference, never the press
            self.on_event('agent_reference', f'press reference not kept: {error}', level='warn')

    def say(self, text: str):
        """Say one sentence for the agent leaf (e.g. why a call failed)"""
        self._say(text)

    def _say(self, text: str | None):
        if text and self.speak:
            self.speak(text)
            self.on_event('agent_say', text)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def _task(phase: str, inputs: dict) -> str:
    lines = [f'Phase: {phase}.']

    if phase == 'judge':
        lines.append('press_button reported success. Judge from the press frames whether the button was pressed; the '
                     'press frame is the fingertip at its closest point to the button.')
    elif phase == 'diagnose':
        lines.append('The press did not work: press_button failed, or it succeeded and press_judge said not_pressed; '
                     'the failure says which. The press ran only after button_in_view and check_reach passed: the stance '
                     'reach. Decide why it failed and what the tree does next.')
    elif phase == 'confirm':
        lines += [f'Question: {inputs.get("question", "")}',
                  'The operator answers in the agent bridge: confirmed when they say yes, not_confirmed when they say no. '
                  'Without a reply the call times out.']
    elif phase == 'stance_check':
        lines.append('The walk to the panel has arrived and nothing has been pressed yet. button_in_view or check_reach '
                     'failed: the failure says which and what it measured; the stance reach_move is the reposition the IK '
                     'miss asks for. Decide whether the robot stands where it can press.')

    if inputs.get('target_description'):
        lines.append(f'Button: {inputs["target_description"]}.')

    if inputs.get('base_goal') is not None:
        goal = inputs['base_goal']
        lines.append(f'Base goal (map): x {goal["x"]:.3f} m, y {goal["y"]:.3f} m, yaw {goal.get("yaw", 0.0):.3f} rad. '
                     'A reposition offset is relative to it, not to where the robot stands.')

    lines.append(f'Allowed decisions: {", ".join(PHASE_DECISIONS[phase])}.')

    return '\n'.join(lines)
