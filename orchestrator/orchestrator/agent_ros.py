"""Read-only ROS access for agent skills, on the executor's node: TF, one message from a topic, the topic list, images.

Called from the agent's thread while the executor spins its node in the main thread. It creates subscriptions
and a TF listener, and never a publisher.
"""

import math
import time
import threading

from typing import Callable
from collections import deque

from rclpy.node import Node
from rclpy.time import Time
from rclpy.duration import Duration
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rosidl_runtime_py import message_to_ordereddict
from rosidl_runtime_py.utilities import get_message
from visualization_msgs.msg import ImageMarker

from .images import draw_outlines
from .press_capture import stamp_seconds


# Topics too big to read whole into the conversation; images go through image()
HEAVY_TYPES = {
    'sensor_msgs/msg/Image', 'sensor_msgs/msg/CompressedImage', 'sensor_msgs/msg/PointCloud2',
    'sensor_msgs/msg/LaserScan', 'nav_msgs/msg/OccupancyGrid', 'livox_ros_driver2/msg/CustomMsg',
}
OUTLINE_TOPIC    = '/track_3d/mask_outlines'    # track_anything: the mask outlines of a frame, only while subscribed
PENDING_OUTLINES = 10                            # outlines that wait for their frame: more than 1 s at 6 fps
MAX_LIST       = 32                 # longer sequences in a message are cut to their first items


class RosReadError(RuntimeError):
    pass


def shrink(value, depth: int = 0):
    """A message dict small enough for the conversation: long sequences and byte arrays are summarized"""
    if isinstance(value, (bytes, bytearray)):
        return f'<{len(value)} bytes>'

    if isinstance(value, dict):
        return {key: shrink(item, depth + 1) for key, item in value.items()}

    if isinstance(value, (list, tuple)) or hasattr(value, 'tolist'):
        items = list(value.tolist() if hasattr(value, 'tolist') else value)

        if len(items) > MAX_LIST:
            return [shrink(item, depth + 1) for item in items[:MAX_LIST]] + [f'... {len(items) - MAX_LIST} more']

        return [shrink(item, depth + 1) for item in items]

    if isinstance(value, float):
        return round(value, 6)

    return value


def _quaternion_to_rpy(x: float, y: float, z: float, w: float) -> list[float]:
    roll  = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw   = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))

    return [round(roll, 6), round(pitch, 6), round(yaw, 6)]


class RosReader:
    def __init__(self, node: Node, latest_camera_image: Callable[[], bytes | None],
                 capture_frames: Callable[[], dict] = dict, frame_at: Callable[[float], bytes | None] = lambda t: None):
        self.node                = node
        self.frame_at            = frame_at             # the camera frame with this stamp (s), as JPEG, from the last seconds
        self.latest_camera_image = latest_camera_image     # the last camera frame, as JPEG
        self.capture_frames      = capture_frames       # the last press's kept frames, by name
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

    # ----- topics -----

    def topic_list(self) -> list[dict]:
        return [{'topic': name, 'types': types} for name, types in sorted(self.node.get_topic_names_and_types())]

    def topic_once(self, topic: str, timeout_s: float = 2.0) -> dict:
        type_name = self._type_of(topic)

        if type_name in HEAVY_TYPES:
            raise RosReadError(f'{topic} is a {type_name}; use get_image for images')

        message = self._once(topic, get_message(type_name), timeout_s)

        return {'topic': topic, 'type': type_name, 'message': shrink(message_to_ordereddict(message))}

    def _type_of(self, topic: str) -> str:
        for name, types in self.node.get_topic_names_and_types():
            if name == topic:
                return types[0]

        raise RosReadError(f'no topic {topic}')

    def _qos_for(self, topic: str) -> QoSProfile:
        """Match the publishers, so a latched topic gives its last value and a best-effort one still arrives"""
        publishers = self.node.get_publishers_info_by_topic(topic)

        if not publishers:
            raise RosReadError(f'nobody publishes {topic}')

        latched  = all(info.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for info in publishers)
        reliable = all(info.qos_profile.reliability == ReliabilityPolicy.RELIABLE for info in publishers)

        return QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE
        )

    def _once(self, topic: str, message_type, timeout_s: float):
        received = threading.Event()
        holder   = []

        def on_message(message):
            if not holder:
                holder.append(message)
                received.set()

        subscription = self.node.create_subscription(message_type, topic, on_message, self._qos_for(topic))

        try:
            if not received.wait(timeout_s):
                raise RosReadError(f'no message on {topic} within {timeout_s:g} s')
        finally:
            self.node.destroy_subscription(subscription)

        return holder[0]

    # ----- images -----

    def _overlay(self, timeout_s: float = 3.0) -> tuple[bytes, str]:
        """The mask outlines drawn on the very frame they come from. track_3d stamps them with that frame's header and
        sends them for each frame it processes (empty when nothing is tracked), not for every camera frame; and the
        frame may reach the executor's buffer after its outlines. So the last outlines wait here for their frame"""
        markers      = deque(maxlen=PENDING_OUTLINES)
        subscription = self.node.create_subscription(ImageMarker, OUTLINE_TOPIC, lambda message: markers.append(message),
                                                     self._qos_for(OUTLINE_TOPIC))   # rclpy cannot inspect a builtin
        deadline     = time.monotonic() + timeout_s

        try:
            while time.monotonic() < deadline:
                for marker in list(markers):
                    frame = self.frame_at(stamp_seconds(marker.header.stamp))

                    if frame is not None:
                        colors = [(c.r, c.g, c.b) for c in marker.outline_colors or [marker.outline_color] * len(marker.points)]
                        return draw_outlines(frame, [(p.x, p.y) for p in marker.points], colors)

                time.sleep(0.05)
        finally:
            self.node.destroy_subscription(subscription)

        raise RosReadError(f'no {OUTLINE_TOPIC} for a camera frame the executor has, within {timeout_s:g} s')

    def image(self, source: str) -> tuple[bytes, str]:
        if source == 'camera':
            frame = self.latest_camera_image()

            if frame is None:
                raise RosReadError('no camera frame yet')

            return frame, 'image/jpeg'

        if source == 'overlay':
            return self._overlay()

        if source.startswith('press_'):
            frame = self.capture_frames().get(source[len('press_'):])

            if frame is None:
                raise RosReadError(f'the last press kept no {source[len("press_"):]} frame')

            return frame, 'image/jpeg'

        raise RosReadError(f'unknown image source {source}')
