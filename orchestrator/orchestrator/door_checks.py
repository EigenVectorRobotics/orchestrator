"""The lidar check of an elevator door: few points in the doorway means the door is open. No ROS here.

The doorway is a box about a place, the door: x along its yaw (out of the cabin), y across, and z measured from the
robot's own base_link height, so that one door place serves every floor of a shaft. A closed door is a wall of points
in that box; an open one leaves only what stands beyond the box. The points are the map-frame scans of the last
window_s, joined: one Mid-360 scan is sparse.
"""

import math

import numpy as np


DOOR_DEFAULTS = {
    'window_s':       1.0,      # join the scans of this long
    'half_depth_m':   0.15,     # along the door's yaw, either side of the door plane
    'half_width_m':   0.4,      # across, either side of the centre: keep it inside the door frame
    'min_up_m':       0.1,      # z above the robot's base_link: the head lidar sees no lower than about here
    'max_up_m':       1.2,
    'max_points':     30,       # at most this many points in the box: open
    'min_scan_points': 1000,    # fewer points in all the window's scans: the lidar is not seen, no judgement
    'max_distance_m': 4.0,      # the door farther than this: wrong place or wrong floor, no judgement
}


def _limits(limits: dict | None, inputs: dict | None) -> dict:
    merged = dict(DOOR_DEFAULTS)
    merged.update(limits or {})
    merged.update({key: value for key, value in (inputs or {}).items() if key in DOOR_DEFAULTS and value is not None})

    return {key: float(value) for key, value in merged.items()}


def move_points(points, translation: tuple, quaternion: tuple):
    """N x 3 points into another frame: rotate by the quaternion (x, y, z, w), then shift"""
    x, y, z, w = quaternion
    rotation   = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ])

    return np.asarray(points, dtype=float) @ rotation.T + np.asarray(translation, dtype=float)


def judge_doorway(points, door: dict | None, robot: tuple | None, limits: dict | None = None,
                  inputs: dict | None = None) -> tuple[bool, str, dict]:
    """(open, message, outputs). points: N x 3 in the map frame (or None: no scan yet); door: {x, y, yaw};
    robot: (x, y, z) of base_link in the map frame"""
    limit = _limits(limits, inputs)

    if door is None:
        return False, 'no door place', {}

    if robot is None:
        return False, 'no robot pose in the map frame', {}

    count = 0 if points is None else len(points)

    if count < limit['min_scan_points']:
        return False, f'only {count} scan points in {limit["window_s"]:g} s (needs {limit["min_scan_points"]:.0f}): no judgement', {'scan_points': count}

    distance = math.hypot(door['x'] - robot[0], door['y'] - robot[1])

    if distance > limit['max_distance_m']:
        return False, f'the door is {distance:.2f} m away (at most {limit["max_distance_m"]:g}): no judgement', {'distance_m': round(distance, 3)}

    points  = np.asarray(points, dtype=float)
    cos_yaw, sin_yaw = math.cos(door['yaw']), math.sin(door['yaw'])
    dx, dy  = points[:, 0] - door['x'], points[:, 1] - door['y']
    along   = dx * cos_yaw + dy * sin_yaw
    across  = -dx * sin_yaw + dy * cos_yaw
    up      = points[:, 2] - robot[2]
    inside  = ((np.abs(along) <= limit['half_depth_m']) & (np.abs(across) <= limit['half_width_m'])
               & (up >= limit['min_up_m']) & (up <= limit['max_up_m']))
    in_box  = int(inside.sum())
    is_open = in_box <= limit['max_points']
    outputs = {'points_in_doorway': in_box, 'scan_points': count, 'distance_m': round(distance, 3),
               'limits': {key: limit[key] for key in ('half_depth_m', 'half_width_m', 'min_up_m', 'max_up_m', 'max_points')}}

    return is_open, (f'{"open" if is_open else "closed"}: {in_box} points in the doorway in {limit["window_s"]:g} s '
                     f'(open at {limit["max_points"]:.0f} or fewer), door {distance:.2f} m away'), outputs
