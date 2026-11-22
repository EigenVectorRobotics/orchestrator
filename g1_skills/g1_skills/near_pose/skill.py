import math

from orchestrator.skills.api import DoneHandle, Handle, Skill

from ..lib.walk import APPROACH_BACK_M, WALK_MIN_M, offset_goal


TF_TIMEOUT_S = 1.0


def near(robot: dict, goal: dict) -> tuple[bool, float, dict]:
    """Whether robot {x, y} stands under WALK_MIN_M from goal, the distance, and the approach APPROACH_BACK_M behind goal"""
    distance = math.hypot(float(goal['x']) - robot['x'], float(goal['y']) - robot['y'])

    return distance < WALK_MIN_M, distance, offset_goal(goal, -APPROACH_BACK_M, 0.0, 0.0)


class NearPose(Skill):
    def start(self, inputs: dict) -> Handle:
        goal = inputs['goal']

        if self.ctx.tf is None:
            return DoneHandle(False, 'no TF here: walk straight to the goal')

        try:
            translation = self.ctx.tf.numbers(self.ctx.tf.lookup('map', 'pelvis', TF_TIMEOUT_S))['translation_m']
        except Exception as error:                      # no transform: the look walks straight to the goal
            return DoneHandle(False, f'no map to pelvis transform ({error}): walk straight to the goal')

        is_near, distance, approach = near({'x': translation[0], 'y': translation[1]}, goal)
        outputs                     = {'approach': approach, 'distance_m': round(distance, 3)}

        if is_near:
            return DoneHandle(True, f'{distance:.3f} m from the goal, under {WALK_MIN_M:g} m: walk out '
                                    f'{APPROACH_BACK_M:g} m behind it first', outputs)

        return DoneHandle(False, f'{distance:.3f} m from the goal: the walk takes steps', outputs)
