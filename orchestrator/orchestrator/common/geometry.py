"""Pose math shared by the executor's modules: quaternions (x, y, z, w), frame transforms and planar moves.

No ROS and no numpy, so the stance checks, the stance evidence and the agent's tools can use it in tests.
press_button's capture keeps its own numpy version for the per-sample phase tracking.
"""

import math


def rotate(quaternion: tuple, vector: tuple) -> tuple:
    """vector rotated by the unit quaternion (x, y, z, w)"""
    qx, qy, qz, qw = quaternion
    vx, vy, vz     = vector
    # t = 2 q_v x v; v' = v + w t + q_v x t
    tx, ty, tz = 2 * (qy * vz - qz * vy), 2 * (qz * vx - qx * vz), 2 * (qx * vy - qy * vx)

    return (vx + qw * tx + (qy * tz - qz * ty),
            vy + qw * ty + (qz * tx - qx * tz),
            vz + qw * tz + (qx * ty - qy * tx))


def multiply(a: tuple, b: tuple) -> tuple:
    """The quaternion product a b: rotate by b, then by a"""
    ax, ay, az, aw = a
    bx, by, bz, bw = b

    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def transform_pose(position: tuple, quaternion: tuple, translation: tuple, rotation: tuple) -> tuple[tuple, tuple]:
    """A pose given in the source frame, in the target frame, with the transform (translation, rotation) of the
    source frame in the target frame (what tf2's lookup_transform(target, source) returns)"""
    moved = rotate(rotation, position)

    return tuple(m + t for m, t in zip(moved, translation)), multiply(rotation, quaternion)


def to_rpy(quaternion: tuple) -> tuple[float, float, float]:
    """Roll, pitch and yaw (rad) of the unit quaternion (x, y, z, w)"""
    x, y, z, w = quaternion
    roll  = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw   = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))

    return roll, pitch, yaw


def yaw_quaternion(yaw: float) -> tuple:
    """The quaternion (x, y, z, w) of a turn by yaw about z"""
    return 0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)


def wrap(angle: float) -> float:
    """angle in (-pi, pi]"""
    return math.atan2(math.sin(angle), math.cos(angle))


def turn_2d(yaw: float, forward: float, left: float) -> tuple[float, float]:
    """A step (forward, left) in a frame turned by yaw, as (x, y) in the frame it is turned in"""
    return math.cos(yaw) * forward - math.sin(yaw) * left, math.sin(yaw) * forward + math.cos(yaw) * left
