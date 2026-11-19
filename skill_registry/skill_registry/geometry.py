"""Pose math: any ROS pose message to the one pose shape this system holds ({x, y, z, yaw, frame}), and back"""

import math

from geometry_msgs.msg import PoseStamped


def yaw_of(orientation) -> float:
    return math.atan2(2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
                      1.0 - 2.0 * (orientation.y ** 2 + orientation.z ** 2))


def frame_of(message, default: str = 'map') -> str:
    """The frame of a message that has a header; the default for one that has none"""
    return getattr(getattr(message, 'header', None), 'frame_id', '') or default


def pose_dict(pose, frame: str = 'map') -> dict:
    """Any pose message as the pose this package holds ({x, y, z, yaw, frame}), the inverse of to_pose_stamped: a
    place, a goal input and a pose output all have the one shape. It unwraps whatever wraps a Pose (PoseStamped,
    PoseWithCovariance, PoseWithCovarianceStamped), and a header on the way names the frame"""
    while hasattr(pose, 'pose'):
        frame = frame_of(pose, frame)
        pose  = pose.pose

    return {'x': pose.position.x, 'y': pose.position.y, 'z': pose.position.z,
            'yaw': yaw_of(pose.orientation), 'frame': frame}


def to_pose_stamped(pose: dict, node) -> PoseStamped:
    """node: anything with get_clock(), for the stamp"""
    message = PoseStamped()
    yaw     = float(pose.get('yaw', 0.0))

    message.header.frame_id     = pose.get('frame', 'map')
    message.header.stamp        = node.get_clock().now().to_msg()
    message.pose.position.x     = float(pose['x'])
    message.pose.position.y     = float(pose['y'])
    message.pose.position.z     = float(pose.get('z', 0.0))     # the storey on a multi-storey map; 0 = the lowest
    message.pose.orientation.z  = math.sin(yaw / 2)
    message.pose.orientation.w  = math.cos(yaw / 2)

    return message
