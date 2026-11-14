"""Read-only ROS access for agent calls, on the executor's node: TF and the latest camera image.

Called from the agent's thread while the executor spins its node in the main thread. It creates a TF listener and
never a publisher.
"""

import math
import threading

from typing import Callable

from rclpy.node import Node
from rclpy.time import Time
from rclpy.duration import Duration


class RosReadError(RuntimeError):
    pass


def _quaternion_to_rpy(x: float, y: float, z: float, w: float) -> list[float]:
    roll  = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw   = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))

    return [round(roll, 6), round(pitch, 6), round(yaw, 6)]


class RosReader:
    def __init__(self, node: Node, latest_camera_image: Callable[[], bytes | None]):
        self.node                = node
        self.latest_camera_image = latest_camera_image     # the last camera frame, as JPEG
        self._tf_buffer          = None
        self._tf_listener        = None
        self._lock               = threading.Lock()

    # ----- TF -----

    def tf(self, target_frame: str, source_frame: str, timeout_s: float = 2.0) -> dict:
        if not target_frame or not source_frame:
            raise RosReadError('tf needs target_frame and source_frame')

        buffer = self._buffer()
        waited = 0.0

        while not buffer.can_transform(target_frame, source_frame, Time()) and waited < timeout_s:
            threading.Event().wait(0.05)
            waited += 0.05

        try:
            transform = buffer.lookup_transform(target_frame, source_frame, Time())
        except Exception as error:                      # tf2 raises several kinds; all mean "not available"
            raise RosReadError(f'no transform {target_frame} <- {source_frame}: {error}') from error

        translation, rotation = transform.transform.translation, transform.transform.rotation
        age_s                 = (self.node.get_clock().now() - Time.from_msg(transform.header.stamp)).nanoseconds * 1e-9

        return {
            'target_frame':       target_frame,
            'source_frame':       source_frame,
            'translation_m':      [round(translation.x, 6), round(translation.y, 6), round(translation.z, 6)],
            'rotation_xyzw':      [round(rotation.x, 6), round(rotation.y, 6), round(rotation.z, 6), round(rotation.w, 6)],
            'rpy_rad':            _quaternion_to_rpy(rotation.x, rotation.y, rotation.z, rotation.w),
            'age_s':              round(age_s, 3),
            'meaning':            f'the pose of {source_frame} in {target_frame}',
        }

    def _buffer(self):
        with self._lock:
            if self._tf_buffer is None:
                from tf2_ros import Buffer, TransformListener

                self._tf_buffer   = Buffer(cache_time=Duration(seconds=10))
                self._tf_listener = TransformListener(self._tf_buffer, self.node)

            return self._tf_buffer

    # ----- images -----

    def image(self, source: str) -> tuple[bytes, str]:
        if source != 'camera':
            raise RosReadError(f'unknown image source {source}')

        frame = self.latest_camera_image()

        if frame is None:
            raise RosReadError('no camera frame yet')

        return frame, 'image/jpeg'
