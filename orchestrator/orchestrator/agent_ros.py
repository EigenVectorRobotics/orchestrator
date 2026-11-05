"""Read-only ROS access for agent skills, on the executor's node: TF, one message from a topic, the topic list, images.

Called from the agent's thread while the executor spins its node in the main thread. It creates subscriptions
and a TF listener, and never a publisher.
"""

import io
import math
import zlib
import struct
import threading

from typing import Callable

import numpy as np

from rclpy.node import Node
from rclpy.time import Time
from rclpy.duration import Duration
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rosidl_runtime_py import message_to_ordereddict
from rosidl_runtime_py.utilities import get_message
from sensor_msgs.msg import CompressedImage, Image


# Topics too big to read whole into the conversation; images go through image()
HEAVY_TYPES = {
    'sensor_msgs/msg/Image', 'sensor_msgs/msg/CompressedImage', 'sensor_msgs/msg/PointCloud2',
    'sensor_msgs/msg/LaserScan', 'nav_msgs/msg/OccupancyGrid', 'livox_ros_driver2/msg/CustomMsg',
}
OVERLAY_TOPIC  = '/track_3d/tracked_overlay'
MAX_LIST       = 32                 # longer sequences in a message are cut to their first items
MAX_IMAGE_SIDE = 1280


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


def _png(rgb: np.ndarray) -> bytes:
    """A minimal PNG writer, for when neither Pillow nor OpenCV is there"""
    height, width, _ = rgb.shape
    raw              = b''.join(b'\0' + rgb[row].tobytes() for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def encode_rgb(rgb: np.ndarray) -> tuple[bytes, str]:
    """JPEG through Pillow or OpenCV, else PNG; the long side cut to MAX_IMAGE_SIDE by striding"""
    step = max(1, math.ceil(max(rgb.shape[:2]) / MAX_IMAGE_SIDE))
    rgb  = np.ascontiguousarray(rgb[::step, ::step])

    try:
        from PIL import Image as PilImage

        buffer = io.BytesIO()
        PilImage.fromarray(rgb).save(buffer, format='JPEG', quality=85)

        return buffer.getvalue(), 'image/jpeg'
    except ImportError:
        pass

    try:
        import cv2

        ok, encoded = cv2.imencode('.jpg', rgb[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 85])

        if ok:
            return encoded.tobytes(), 'image/jpeg'
    except ImportError:
        pass

    return _png(rgb), 'image/png'


def image_to_rgb(message: Image) -> np.ndarray:
    channels = {'rgb8': 3, 'bgr8': 3, 'rgba8': 4, 'bgra8': 4, 'mono8': 1}.get(message.encoding)

    if channels is None:
        raise RosReadError(f'image encoding {message.encoding} is not supported')

    pixels = np.frombuffer(bytes(message.data), dtype=np.uint8).reshape(message.height, message.step)
    pixels = pixels[:, :message.width * channels].reshape(message.height, message.width, channels)

    if message.encoding == 'mono8':
        return np.repeat(pixels, 3, axis=2)

    if message.encoding.startswith('bgr'):
        pixels = pixels[:, :, 2::-1]

    return pixels[:, :, :3]


class RosReader:
    def __init__(self, node: Node, latest_camera_image: Callable[[], CompressedImage | None],
                 capture_frames: Callable[[], dict] = dict):
        self.node                = node
        self.latest_camera_image = latest_camera_image
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

    def image(self, source: str) -> tuple[bytes, str]:
        if source == 'camera':
            frame = self.latest_camera_image()

            if frame is None:
                raise RosReadError('no camera frame yet')

            media_type = 'image/png' if 'png' in frame.format.lower() else 'image/jpeg'

            return bytes(frame.data), media_type

        if source == 'overlay':
            return encode_rgb(image_to_rgb(self._once(OVERLAY_TOPIC, Image, 3.0)))

        if source.startswith('press_'):
            frame = self.capture_frames().get(source[len('press_'):])

            if frame is None:
                raise RosReadError(f'the last press kept no {source[len("press_"):]} frame')

            return frame, 'image/jpeg'

        raise RosReadError(f'unknown image source {source}')
