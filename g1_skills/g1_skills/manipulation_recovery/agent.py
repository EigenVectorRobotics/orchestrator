from orchestrator.agent.api import AgentCall, AgentSkill, CallImage

from ..lib.press import offset_in_force, press_images, press_state
from . import stance
from .rules import decision_outputs, rule_reach, rule_stance, stance_unchanged
from .spec import MAX_PRESS_OFFSET_M, REPOSITION_LIMITS_M


PRESS_FRAME = 'foot_world'              # press_button's goal frame (move_to_pose planning_frame)
RULE_SPEECH = {
    'stance': 'I am not standing where I should. Walking to my spot again.',
    'reach':  'The button is just out of my reach. Stepping closer to it.',
}


class ManipulationRecovery(AgentSkill):
    """Why a press failed and what the tree does next (diagnose), or whether the stance before the first press is fine
    (stance_check). The executor's rules answer a plain stance first (rules.py); a missed press always goes to the model,
    with crops of the press around the button for its offset_retry"""

    phase_speech = {
        'diagnose':     'The press did not work. Checking why.',
        'stance_check': 'I cannot see the button well from here. Checking where I stand.',
    }
    decision_speech = {
        'reposition':   'Moving to a better spot.',
        'switch_arm':   'Switching to my other arm.',
        'offset_retry': 'Correcting my aim and pressing again.',
        'retry':        'Trying the press again.',
        'abort':        'I cannot press this button. Stopping the task.',
        'continue':     'My stance looks fine. Looking at the button again from here.',
    }

    def refuse(self, call: AgentCall) -> tuple[str, str] | None:
        """After ask_help at this panel, or max_rounds diagnoses in the task, the skill fails without a call"""
        decisions = [record for record in call.context.history if record.get('skill') == self.contract.name]
        here      = call.inputs.get('base_goal')
        gave_up   = next((record for record in decisions if record.get('decision') == 'ask_help'
                          and record.get('base_goal') == here), None)
        rounds    = sum(1 for record in decisions if record.get('phase') == 'diagnose' and not record.get('rule'))

        if gave_up is not None:
            return f'the agent already asked for help: {gave_up.get("reason", "")}', ''     # said once already

        if call.phase == 'diagnose' and rounds >= int(call.inputs.get('max_rounds', 5)):
            return (f'recovery budget spent: {rounds} diagnoses in this task',
                    'I have used up my recovery attempts for this task.')

        return None

    def evidence(self, call: AgentCall, evidence: dict):
        if call.phase == 'diagnose' and (evidence['last_failure'] or {}).get('skill') == 'press_judge':
            evidence['press'] = press_state(self.ctx).evidence()        # the judged press: its frames are chosen already

        try:
            evidence['stance'] = self._stance(call.inputs, evidence)
        except Exception as error:                      # a missing transform leaves the call without a stance, not failed
            evidence['stance'] = {'error': str(error)}

    def rule(self, call: AgentCall, evidence: dict) -> tuple[dict, str] | None:
        found = rule_stance(evidence.get('stance'), evidence['history'])

        if found is None and call.phase == 'stance_check':
            found = rule_reach(evidence.get('stance'), evidence['history'], evidence['last_failure'])

        return (found, RULE_SPEECH[found['rule_name']]) if found is not None else None

    def task_lines(self, call: AgentCall) -> list[str]:
        if call.phase == 'diagnose':
            lines = ['The press did not work: press_button failed, or it succeeded and press_judge said not_pressed; '
                     'the failure says which. The press ran only after button_in_view and check_reach passed: from this '
                     'stance the arm reached the button at the check. Decide why it failed and what the tree does next.']
        else:
            lines = ['The walk to the panel has arrived and nothing has been pressed yet. button_in_view or check_reach '
                     'failed: the failure says which and what it measured; the stance reach_move is the reposition the IK '
                     'miss asks for. Decide whether the robot stands where it can press.']

        return lines + button_lines(call.inputs)

    def request_fields(self, call: AgentCall, evidence: dict) -> dict:
        press  = evidence.get('press') or None
        fields = {
            'button':          call.inputs.get('target_description', ''),
            'base_goal':       call.inputs.get('base_goal'),
            'offset_in_force': offset_in_force(evidence['history'], (press or {}).get('press_offset_m')),
            'stance':          evidence.get('stance') or None,
        }

        if call.phase == 'diagnose':
            fields['press'] = press

        return fields

    def images(self, request: dict, session) -> list[CallImage]:
        """The judged press's frames and aim crops, and the camera image now"""
        images = press_images(request.get('press'), aim=True)

        if (camera := session.camera()) is not None:
            images.append(CallImage('camera', camera[1], camera[0]))

        return images

    def check(self, decision: dict, session) -> str:
        name = decision['decision']

        if name == 'reposition':
            if session.inputs.get('base_goal') is None:
                return 'this call has no base_goal, so a reposition cannot be carried out; choose another decision'

            if decision.get('dx') is None or decision.get('dy') is None:
                return 'reposition needs dx and dy (dyaw defaults to 0)'

            dx, dy = decision['dx'], decision['dy']

            if session.phase == 'diagnose' and (unchanged := stance_unchanged(session.evidence.get('stance'))):
                return unchanged

            if dx > REPOSITION_LIMITS_M['forward'] or dx < -REPOSITION_LIMITS_M['back'] or abs(dy) > REPOSITION_LIMITS_M['side']:
                return (f'a reposition stays within {REPOSITION_LIMITS_M["forward"]:g} m past the base goal, '
                        f'{REPOSITION_LIMITS_M["back"]:g} m behind it and {REPOSITION_LIMITS_M["side"]:g} m to the side: '
                        'the base goal stands close to the panel, so more puts the robot into it. A target that looks '
                        'farther than that is a wrong detection, not a wrong stance: choose another decision')

        if name == 'switch_arm' and decision.get('use_left_arm') is None:
            return 'switch_arm needs use_left_arm'

        if name == 'offset_retry':
            offset = [decision.get(key) for key in ('ox', 'oy', 'oz')]

            if any(value is None for value in offset):
                return 'offset_retry needs ox, oy and oz: the whole press offset to use, in metres'

            if max(abs(value) for value in offset) > MAX_PRESS_OFFSET_M:
                return (f'offset_retry corrects at most {MAX_PRESS_OFFSET_M * 1000:g} mm per axis; a larger miss is a wrong '
                        'target or a wrong stance: choose another decision')

        for key in ('dx', 'dy', 'dyaw'):
            decision[key] = decision.get(key) or 0.0

        return ''

    def outputs(self, decision: dict, call: AgentCall, evidence: dict) -> dict:
        return decision_outputs(decision, call.inputs, evidence['last_failure'])

    def record_fields(self, outputs: dict, call: AgentCall) -> dict:
        return {'offset': outputs.get('offset'), 'goal': outputs.get('goal'), 'base_goal': call.inputs.get('base_goal'),
                'left_arm': outputs.get('left_arm')}

    def _stance(self, inputs: dict, evidence: dict) -> dict | None:
        """The robot against the base goal and against the last press on this button that worked"""
        if self.ctx.tf is None:
            return None

        tf      = lambda target, source, timeout_s: frame_pose(self.ctx, target, source, timeout_s)
        failure = evidence.get('last_failure') or {}
        goal    = stance.last_goal(failure)
        call    = failure.get('checked_call') or failure
        robot   = stance.pose_2d(tf('map', 'pelvis', 1.0))
        button  = inputs.get('target_description', '')

        return stance.evidence(inputs.get('base_goal'), robot,
                               stance.transform_point(tf('pelvis', PRESS_FRAME, 1.0), goal) if goal else None,
                               stance.transform_point(tf('map', PRESS_FRAME, 1.0), goal) if goal else None,
                               (call.get('inputs') or {}).get('left_arm'), button, stance.load_references(),
                               stance.latest_reach(failure, evidence.get('history')), REPOSITION_LIMITS_M)

def frame_pose(ctx, target_frame: str, source_frame: str, timeout_s: float) -> dict:
    """The pose of source_frame in target_frame as plain numbers, as the stance evidence reads it"""
    return ctx.tf.numbers(ctx.tf.lookup(target_frame, source_frame, timeout_s))


def button_lines(inputs: dict) -> list[str]:
    """The button and the base goal of a press call, as task text"""
    lines = []

    if inputs.get('target_description'):
        lines.append(f'Button: {inputs["target_description"]}.')

    if inputs.get('base_goal') is not None:
        goal = inputs['base_goal']
        lines.append(f'Base goal (map): x {goal["x"]:.3f} m, y {goal["y"]:.3f} m, yaw {goal.get("yaw", 0.0):.3f} rad. '
                     'A reposition offset is relative to it, not to where the robot stands.')

    return lines
