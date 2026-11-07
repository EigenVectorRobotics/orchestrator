"""Pure functions for sim_orin.py: frames, the Mid-360 ray pattern, Livox CustomMsg CDR packing and IMU math."""
import math
import struct

import numpy as np

# Mid-360 vertical field of view in its own frame [deg]
MID360_MIN_ELEVATION = -7.0
MID360_MAX_ELEVATION = 52.0

# One CustomPoint in CDR: uint32 offset_time, float32 x y z, uint8 reflectivity tag line, then 1 byte to align the next point
CUSTOM_POINT_DTYPE = np.dtype([
    ("offset_time", "<u4"),
    ("x", "<f4"),
    ("y", "<f4"),
    ("z", "<f4"),
    ("reflectivity", "u1"),
    ("tag", "u1"),
    ("line", "u1"),
    ("pad", "u1"),
])


def rpy_matrix(
    roll: float,
    pitch: float,
    yaw: float,
) -> np.ndarray:
    """Rotation matrix of URDF fixed-axis roll, pitch, yaw."""
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)

    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])


def transform(
    rotation: np.ndarray,
    translation,
) -> np.ndarray:
    """4x4 homogeneous transform from a rotation matrix and a translation."""
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = translation

    return matrix


def quaternion_wxyz_matrix(
    quaternion: np.ndarray,
) -> np.ndarray:
    """Rotation matrix of a unit quaternion in w, x, y, z order."""
    w, x, y, z = quaternion

    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def matrix_quaternion_xyzw(
    rotation: np.ndarray,
) -> tuple[float, float, float, float]:
    """Unit quaternion (x, y, z, w) of a rotation matrix."""
    trace = rotation[0, 0] + rotation[1, 1] + rotation[2, 2]

    if trace > 0:
        s = 2.0 * math.sqrt(trace + 1.0)
        return ((rotation[2, 1] - rotation[1, 2]) / s, (rotation[0, 2] - rotation[2, 0]) / s,
                (rotation[1, 0] - rotation[0, 1]) / s, 0.25 * s)

    if rotation[0, 0] > rotation[1, 1] and rotation[0, 0] > rotation[2, 2]:
        s = 2.0 * math.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2])
        return (0.25 * s, (rotation[0, 1] + rotation[1, 0]) / s, (rotation[0, 2] + rotation[2, 0]) / s,
                (rotation[2, 1] - rotation[1, 2]) / s)

    if rotation[1, 1] > rotation[2, 2]:
        s = 2.0 * math.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2])
        return ((rotation[0, 1] + rotation[1, 0]) / s, 0.25 * s, (rotation[1, 2] + rotation[2, 1]) / s,
                (rotation[0, 2] - rotation[2, 0]) / s)

    s = 2.0 * math.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1])
    return ((rotation[0, 2] + rotation[2, 0]) / s, (rotation[1, 2] + rotation[2, 1]) / s, 0.25 * s,
            (rotation[1, 0] - rotation[0, 1]) / s)


def yaw_of(
    rotation: np.ndarray,
) -> float:
    """Heading of a rotation matrix about world z [rad]."""
    return math.atan2(rotation[1, 0], rotation[0, 0])


def mid360_directions(
    count: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Unit ray directions in the lidar frame, uniform over the Mid-360 field of view (it never repeats a pattern)."""
    azimuth = rng.uniform(0.0, 2.0 * math.pi, count)
    sin_elevation = rng.uniform(math.sin(math.radians(MID360_MIN_ELEVATION)),
                                math.sin(math.radians(MID360_MAX_ELEVATION)), count)
    cos_elevation = np.sqrt(1.0 - sin_elevation ** 2)

    return np.stack([cos_elevation * np.cos(azimuth), cos_elevation * np.sin(azimuth), sin_elevation], axis=1)


def pack_custom_msg(
    stamp_ns: int,
    frame_id: str,
    points: np.ndarray,
) -> bytes:
    """Serialized livox_ros_driver2/msg/CustomMsg (XCDR1, little endian); points has CUSTOM_POINT_DTYPE."""
    frame = frame_id.encode() + b"\0"
    body = struct.pack("<iII", stamp_ns // 1_000_000_000, stamp_ns % 1_000_000_000, len(frame)) + frame
    body += b"\0" * (-len(body) % 8)
    body += struct.pack("<QIB3xI", stamp_ns, len(points), 0, len(points))
    data = points.tobytes()

    return b"\x00\x01\x00\x00" + body + (data[:-1] if len(points) else data)


def point_acceleration(
    origin_acceleration: np.ndarray,
    angular_velocity: np.ndarray,
    angular_acceleration: np.ndarray,
    lever: np.ndarray,
) -> np.ndarray:
    """Acceleration of a point on a rigid body at lever from the body origin (all world frame)."""
    return (origin_acceleration + np.cross(angular_acceleration, lever)
            + np.cross(angular_velocity, np.cross(angular_velocity, lever)))
