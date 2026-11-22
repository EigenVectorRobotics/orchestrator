"""What the walking skills share: the shortest walk navigate_to takes a step for, and goals moved in their own frame."""

from orchestrator.common.geometry import turn_2d, wrap


WALK_MIN_M      = 0.10      # navigate_to ends a shorter walk without a step
APPROACH_BACK_M = 0.15      # so a stance closer than WALK_MIN_M is reached by walking out this far behind it first, then onto it


def offset_goal(base: dict, dx: float, dy: float, dyaw: float) -> dict:
    """base moved by (dx, dy) in its own frame (x along its yaw) and turned by dyaw"""
    yaw            = float(base.get('yaw', 0.0))
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
