from orchestrator.agent.api import AgentCall, AgentSkill, CallImage
from orchestrator.tree.engine import Status

from ..lib.press import offset_in_force, press_images, press_state
from ..manipulation_recovery import stance
from ..manipulation_recovery.agent import PRESS_FRAME, frame_pose


class PressJudge(AgentSkill):
    """Looks at the frames press_button kept and judges whether the button was pressed. not_pressed is recorded as the
    failed check of that press, so manipulation_recovery sees it as checked_call; pressed keeps the stance as the
    reference for later presses of the button"""

    phase_speech    = {'judge': 'Checking whether the button was pressed.'}
    decision_speech = {'pressed': 'The button is pressed.', 'not_pressed': 'I missed the button.'}

    def evidence(self, call: AgentCall, evidence: dict):
        """Waits until the press's frames are chosen, then saves them"""
        evidence['press'] = press_state(self.ctx).evidence() if self.ctx is not None else None

    def task_lines(self, call: AgentCall) -> list[str]:
        lines = ['press_button reported success. Judge from the press frames whether the button was pressed; the '
                 'press frame is the fingertip at its closest point to the button.']

        if call.inputs.get('target_description'):
            lines.append(f'Button: {call.inputs["target_description"]}.')

        return lines

    def request_fields(self, call: AgentCall, evidence: dict) -> dict:
        press = evidence.get('press') or None

        return {'button': call.inputs.get('target_description', ''), 'press': press,
                'offset_in_force': offset_in_force(evidence['history'], (press or {}).get('press_offset_m'))}

    def images(self, request: dict, session) -> list[CallImage]:
        return press_images(request.get('press'))

    def after_decision(self, decision: dict, call: AgentCall, evidence: dict):
        if decision['decision'] == 'pressed':
            self._keep_reference(call.inputs, evidence.get('press') or {})

    def finished(self, call: AgentCall, status: Status, outputs: dict, record: dict):
        """A not_pressed after a press that succeeded is the failed check of that press"""
        checked = call.context.last_call

        if status == Status.FAILURE and checked is not None and checked.get('status') == Status.SUCCESS.value:
            record = {**record, 'checks': checked['skill']}
            call.context.record(record, {**record, 'checked_call': checked})

    def _keep_reference(self, inputs: dict, press: dict):
        """A press judged pressed becomes the reference for later presses on this button"""
        if self.ctx is None or self.ctx.tf is None or not press.get('goal'):
            return

        tf = lambda target, source, timeout_s: frame_pose(self.ctx, target, source, timeout_s)

        try:
            stance.save_reference({
                'button':        inputs.get('target_description', ''),
                'left_arm':      bool(press.get('left_arm')),
                'robot':         stance.pose_2d(tf('map', 'pelvis', 1.0)),
                'target_pelvis': stance.transform_point(tf('pelvis', PRESS_FRAME, 1.0), press['goal']),
                'target_map':    stance.transform_point(tf('map', PRESS_FRAME, 1.0), press['goal']),
            })
        except Exception as error:                      # a missing transform costs the reference, never the press
            self.ctx.logger.warn(f'press reference not kept: {error}')
