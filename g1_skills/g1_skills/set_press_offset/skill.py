from orchestrator.skills.api import Skill

from ..lib.press import press_state


class SetPressOffset(Skill):
    """Keeps the offset it sends: move_to_pose holds it until the next one, and press_button and check_reach need it"""

    def before(self, inputs: dict):
        press_state(self.ctx).offset = tuple(float(inputs.get(axis, 0.0)) for axis in 'xyz')
