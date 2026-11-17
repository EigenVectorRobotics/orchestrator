"""Read-only ROS access for agent calls, on the executor's node: TF and the latest camera image.

Called from the agent's thread while the executor spins its node in the main thread. It reads TF through the buffer
it is given (the skill runner's), and never publishes.
"""

import threading

from typing import Callable

from rclpy.node import Node
from rclpy.time import Time

from .geometry import to_rpy


class RosReadError(RuntimeError):
    pass


class RosReader:
    def __init__(self, node: Node, latest_camera_image: Callable[[], bytes | None], tf_buffer: Callable[[], object]):
        self.node                = node
        self.latest_camera_image = latest_camera_image     # the last camera frame, as JPEG
        self._buffer             = tf_buffer               # the executor's one TF buffer (RosSkillRunner._tf)

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
            'rpy_rad':            [round(angle, 6) for angle in to_rpy((rotation.x, rotation.y, rotation.z, rotation.w))],
            'age_s':              round(age_s, 3),
            'meaning':            f'the pose of {source_frame} in {target_frame}',
        }

    # ----- images -----

    def image(self, source: str) -> tuple[bytes, str]:
        if source != 'camera':
            raise RosReadError(f'unknown image source {source}')

        frame = self.latest_camera_image()

        if frame is None:
            raise RosReadError('no camera frame yet')

        return frame, 'image/jpeg'
