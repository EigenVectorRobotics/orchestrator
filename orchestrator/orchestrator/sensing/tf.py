import threading

from rclpy.node import Node
from rclpy.time import Time
from rclpy.duration import Duration
from geometry_msgs.msg import PoseStamped

from ..common.geometry import to_rpy, transform_pose


class TfError(RuntimeError):
    pass


class Tf:
    """The executor's one TF buffer and listener, made on first use; skills and the agent's thread share it"""

    def __init__(self, node: Node):
        self.node     = node
        self._buffer  = None
        self._lock    = threading.Lock()

    def buffer(self):
        with self._lock:
            if self._buffer is None:
                from tf2_ros import Buffer, TransformListener

                self._buffer   = Buffer(cache_time=Duration(seconds=10))
                self._listener = TransformListener(self._buffer, self.node)

            return self._buffer

    def lookup(self, target_frame: str, source_frame: str):
        """The latest TransformStamped; raises when TF has none"""
        return self.buffer().lookup_transform(target_frame, source_frame, Time())

    def pose_in(self, message: PoseStamped, frame: str) -> tuple[tuple, tuple]:
        """The stamped pose's position and rotation in frame, through the latest TF; raises when TF has no transform"""
        pose        = message.pose
        transform   = self.lookup(frame, message.header.frame_id)
        shift, turn = transform.transform.translation, transform.transform.rotation

        return transform_pose(
            (pose.position.x, pose.position.y, pose.position.z),
            (pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w),
            (shift.x, shift.y, shift.z), (turn.x, turn.y, turn.z, turn.w)
        )

    def tf(self, target_frame: str, source_frame: str, timeout_s: float = 2.0) -> dict:
        """The pose of source_frame in target_frame as plain numbers, waiting up to timeout_s; from any thread"""
        if not target_frame or not source_frame:
            raise TfError('tf needs target_frame and source_frame')

        buffer = self.buffer()
        waited = 0.0

        while not buffer.can_transform(target_frame, source_frame, Time()) and waited < timeout_s:
            threading.Event().wait(0.05)
            waited += 0.05

        try:
            transform = buffer.lookup_transform(target_frame, source_frame, Time())
        except Exception as error:                      # tf2 raises several kinds; all mean "not available"
            raise TfError(f'no transform {target_frame} <- {source_frame}: {error}') from error

        translation, rotation = transform.transform.translation, transform.transform.rotation
        age_s                 = (self.node.get_clock().now() - Time.from_msg(transform.header.stamp)).nanoseconds * 1e-9

        return {
            'target_frame':  target_frame,
            'source_frame':  source_frame,
            'translation_m': [round(translation.x, 6), round(translation.y, 6), round(translation.z, 6)],
            'rotation_xyzw': [round(rotation.x, 6), round(rotation.y, 6), round(rotation.z, 6), round(rotation.w, 6)],
            'rpy_rad':       [round(angle, 6) for angle in to_rpy((rotation.x, rotation.y, rotation.z, rotation.w))],
            'age_s':         round(age_s, 3),
            'meaning':       f'the pose of {source_frame} in {target_frame}',
        }
