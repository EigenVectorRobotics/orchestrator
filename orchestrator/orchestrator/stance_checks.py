"""Before the first press: is the button in the camera's view from where the robot stands, and which arm presses it?

A poor stance comes from the walk before the press (experience E-001, E-015): the robot stopped too close, so
the button fell below the frame, or too far, so the arm stops short. These checks catch it before press_button
fails. They run in the executor on what track_anything already publishes (its 2D box, its 3D target pose) and TF,
with no VLM call of their own; button_in_view seeds the track that feeds them.

  in_view   the target's latest box is fresh and lies inside the image, a margin away from each edge

This module needs no ROS.
"""

import math


VIEW_DEFAULTS  = {'max_age_s': 1.0, 'margin_frac': 0.05}
ARM_DEFAULTS   = {'left_arm_min_y_m': 0.10}


def _limits(defaults: dict, limits: dict | None, inputs: dict | None) -> dict:
    """The contract's limits over the defaults, and the tree's inputs over both (a panel's own range)"""
    merged = {**defaults, **(limits or {})}
    merged.update({key: value for key, value in (inputs or {}).items() if key in defaults and value is not None})

    return {key: float(value) for key, value in merged.items()}


def judge_view(box: tuple | None, box_age_s: float | None, image_size: tuple | None, limits: dict | None = None,
               inputs: dict | None = None) -> tuple[bool, str, dict]:
    """box (x_min, y_min, x_max, y_max) in pixels, its age in seconds, image_size (width, height)"""
    limit = _limits(VIEW_DEFAULTS, limits, inputs)

    if image_size is None:
        return False, 'no camera image yet', {}

    if box is None or box_age_s is None or box_age_s > limit['max_age_s']:
        age = 'never' if box_age_s is None else f'{box_age_s:.1f} s ago'
        return False, (f'no fresh box of the target from track_anything (last {age}; needs {limit["max_age_s"]:g} s): '
                       'not tracking, or the button is out of the frame'), {}

    width, height        = image_size
    x_min, y_min, x_max, y_max = box
    margin_x, margin_y   = limit['margin_frac'] * width, limit['margin_frac'] * height
    edges                = {'left': x_min, 'top': y_min, 'right': width - x_max, 'bottom': height - y_max}
    margins              = {'left': margin_x, 'top': margin_y, 'right': margin_x, 'bottom': margin_y}
    close                = [side for side, distance in edges.items() if distance < margins[side]]
    outputs              = {
        'box':          [round(value, 1) for value in box],
        'image_size':   [width, height],
        'centre_frac':  [round((x_min + x_max) / 2 / width, 3), round((y_min + y_max) / 2 / height, 3)],
        'edge_px':      {side: round(distance, 1) for side, distance in edges.items()},
    }
    where = f'box centre at ({outputs["centre_frac"][0]:.2f}, {outputs["centre_frac"][1]:.2f}) of the image'

    if close:
        sides = ', '.join(f'{side} {edges[side]:.0f} px' for side in close)
        return False, f'the target is at the edge of the image ({sides}; needs {limit["margin_frac"]:.0%}): {where}', outputs

    return True, f'in view: {where}', outputs


def judge_arm(position: tuple, limits: dict | None = None, inputs: dict | None = None) -> tuple[bool, str]:
    """True for the left arm: the button sits more than left_arm_min_y_m to the robot's left (E-007); a tree sets it per panel"""
    threshold = _limits(ARM_DEFAULTS, limits, inputs)['left_arm_min_y_m']
    y         = position[1]

    if y > threshold:
        return True, f'left arm: the button is {y:+.3f} m to the side, past {threshold:+.2f}'

    return False, f'right arm: the button is {y:+.3f} m to the side, not past {threshold:+.2f}'


# ========== Poses ==========

def rotate(quaternion: tuple, vector: tuple) -> tuple:
    """vector rotated by the unit quaternion (x, y, z, w)"""
    qx, qy, qz, qw = quaternion
    vx, vy, vz     = vector
    # t = 2 q_v x v; v' = v + w t + q_v x t
    tx, ty, tz = 2 * (qy * vz - qz * vy), 2 * (qz * vx - qx * vz), 2 * (qx * vy - qy * vx)

    return (vx + qw * tx + (qy * tz - qz * ty),
            vy + qw * ty + (qz * tx - qx * tz),
            vz + qw * tz + (qx * ty - qy * tx))


def transform_pose(position: tuple, quaternion: tuple, translation: tuple, rotation: tuple) -> tuple[tuple, tuple]:
    """A pose given in the source frame, in the target frame, with the transform (translation, rotation) of the
    source frame in the target frame (what tf2's lookup_transform(target, source) returns)"""
    moved = rotate(rotation, position)
    ax, ay, az, aw = rotation
    bx, by, bz, bw = quaternion

    product = (aw * bx + ax * bw + ay * bz - az * by,
               aw * by - ax * bz + ay * bw + az * bx,
               aw * bz + ax * by - ay * bx + az * bw,
               aw * bw - ax * bx - ay * by - az * bz)

    return tuple(m + t for m, t in zip(moved, translation)), product

