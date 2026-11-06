"""Before the first press: is the button in the camera's view, and within the arm's reach from where the robot stands?

A poor stance comes from the walk before the press (experience E-001, E-015, E-006): the robot stopped too close, so
the button fell below the frame, or too far, so the arm stops short. These checks catch it before press_button
fails. They run in the executor on what track_anything already publishes (its 2D box, its 3D target pose) and TF,
with no cloud and no VLM call of their own; the track that feeds them is seeded by the track skill.

  in_view   the target's latest box is fresh and lies inside the image, a margin away from each edge
  in_reach  the target's latest pose, in the pelvis, is ahead of it within [min_ahead_m, max_ahead_m], within
            max_sideways_m to the side, and the panel faces the robot within max_slant_deg

The reach limits are a box around what worked on each panel (E-006), not inverse kinematics: a pass means "like
the stances that worked", and a fail means "look before pressing". The agent's stance_check decides from there.

This module needs no ROS.
"""

import math


VIEW_DEFAULTS  = {'max_age_s': 1.0, 'margin_frac': 0.05}
REACH_DEFAULTS = {'max_age_s': 1.0, 'min_ahead_m': 0.33, 'max_ahead_m': 0.48, 'max_sideways_m': 0.15, 'max_slant_deg': 45.0}


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


def judge_reach(position: tuple | None, normal: tuple | None, pose_age_s: float | None, limits: dict | None = None,
                inputs: dict | None = None, frame: str = 'pelvis') -> tuple[bool, str, dict]:
    """position of the button in the robot's frame (x ahead, y left, z up); normal: the direction into the panel,
    in the same frame. The limits say what worked; outside them is a stance to look at before pressing"""
    limit = _limits(REACH_DEFAULTS, limits, inputs)

    if position is None or pose_age_s is None or pose_age_s > limit['max_age_s']:
        age = 'never' if pose_age_s is None else f'{pose_age_s:.1f} s ago'
        return False, (f'no fresh 3D pose of the target (last {age}; needs {limit["max_age_s"]:g} s): not tracking, '
                       'or too close for depth (the D435 sees nothing nearer than about 0.28 m)'), {}

    ahead, sideways, up = position
    slant_deg           = math.degrees(abs(math.atan2(normal[1], normal[0]))) if normal is not None else 0.0
    outputs             = {
        'frame':      frame,
        'ahead_m':    round(ahead, 3),
        'sideways_m': round(sideways, 3),
        'up_m':       round(up, 3),
        'slant_deg':  round(slant_deg, 1),
        'limits':     {key: limit[key] for key in ('min_ahead_m', 'max_ahead_m', 'max_sideways_m', 'max_slant_deg')},
    }
    problems = []

    if ahead < limit['min_ahead_m']:
        problems.append(f'too close: {ahead:.3f} m ahead (needs >= {limit["min_ahead_m"]:.2f})')

    if ahead > limit['max_ahead_m']:
        problems.append(f'too far: {ahead:.3f} m ahead (needs <= {limit["max_ahead_m"]:.2f})')

    if abs(sideways) > limit['max_sideways_m']:
        side = 'left' if sideways > 0 else 'right'
        problems.append(f'{abs(sideways):.3f} m to the {side} (needs <= {limit["max_sideways_m"]:.2f})')

    if slant_deg > limit['max_slant_deg']:
        problems.append(f'the panel is at {slant_deg:.0f} deg to the robot (needs <= {limit["max_slant_deg"]:.0f})')

    where = f'button at {ahead:+.3f} ahead, {sideways:+.3f} left, {up:+.3f} up m in {frame}'

    if problems:
        return False, f'{"; ".join(problems)}: {where}', outputs

    return True, f'in the reach that worked: {where}', outputs
