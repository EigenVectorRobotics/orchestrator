import threading

from rclpy.node import Node
from rclpy.time import Time
from rclpy.duration import Duration
from sensor_msgs.msg import CameraInfo, Image
from geometry_msgs.msg import PoseStamped

from .common.images import ImageError, image_to_jpeg
from .common.topics import SENSOR_QOS
from .common.geometry import to_rpy, transform_pose


class FrameBuffer:
    """The last few seconds of camera frames: (ROS time, JPEG or PNG bytes)"""

    def __init__(self, keep_s: float = 8.0):
        self.keep_s  = keep_s
        self._frames = []
        self._lock   = threading.Lock()

    def add(self, t: float, data: bytes):
        with self._lock:
            self._frames.append((t, data))
            self._frames = [frame for frame in self._frames if frame[0] >= t - self.keep_s]

    def first_after(self, t: float):
        with self._lock:
            return next((frame for frame in self._frames if frame[0] >= t), None)


class Camera:
    """The head camera: its latest frame as JPEG, its size, optical frame and intrinsics, and the last seconds of
    frames by their stamps"""

    def __init__(self, node: Node, topic: str, info_topic: str = '/camera/camera/color/camera_info'):
        self.node     = node
        self.topic    = topic
        self.jpeg     = None
        self.size     = None            # (width, height) of the last frame
        self.frame_id = None            # the images' optical frame, from their header
        self.k        = None            # the intrinsics, 3x3 row-major, from camera_info
        self.frames   = FrameBuffer()

        node.create_subscription(Image, topic, self._on_image, SENSOR_QOS)
        node.create_subscription(CameraInfo, info_topic, self._on_info, SENSOR_QOS)

    def _on_image(self, message: Image):
        """Raw frames from the RealSense, encoded here: the Orin publishes no compressed transport"""
        try:
            jpeg = image_to_jpeg(message)
        except ImageError as error:
            self.node.get_logger().warn(f'camera frame dropped: {error}', throttle_duration_sec=10.0)
            return

        stamp         = message.header.stamp.sec + message.header.stamp.nanosec * 1e-9
        self.jpeg     = jpeg
        self.size     = (message.width, message.height)
        self.frame_id = message.header.frame_id or self.frame_id
        self.frames.add(stamp or self.node.get_clock().now().nanoseconds * 1e-9, jpeg)

    def _on_info(self, message: CameraInfo):
        self.k = [[float(value) for value in message.k[row * 3:row * 3 + 3]] for row in range(3)]


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

    def lookup(self, target_frame: str, source_frame: str, timeout_s: float = 0.0):
        """The latest TransformStamped of source_frame in target_frame, waiting up to timeout_s for it (0: the executor's
        tick, which must not block); raises TfError when TF has none"""
        if not target_frame or not source_frame:
            raise TfError('tf needs target_frame and source_frame')

        buffer = self.buffer()
        waited = 0.0

        while waited < timeout_s and not buffer.can_transform(target_frame, source_frame, Time()):
            threading.Event().wait(0.05)
            waited += 0.05

        try:
            return buffer.lookup_transform(target_frame, source_frame, Time())
        except Exception as error:                      # tf2 raises several kinds; all mean "not available"
            raise TfError(f'no transform {target_frame} <- {source_frame}: {error}') from error

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

    def numbers(self, transform) -> dict:
        """A TransformStamped as plain rounded numbers, for evidence that goes out as JSON"""
        translation, rotation = transform.transform.translation, transform.transform.rotation
        age_s                 = (self.node.get_clock().now() - Time.from_msg(transform.header.stamp)).nanoseconds * 1e-9

        return {
            'target_frame':  transform.header.frame_id,
            'source_frame':  transform.child_frame_id,
            'translation_m': [round(translation.x, 6), round(translation.y, 6), round(translation.z, 6)],
            'rotation_xyzw': [round(rotation.x, 6), round(rotation.y, 6), round(rotation.z, 6), round(rotation.w, 6)],
            'rpy_rad':       [round(angle, 6) for angle in to_rpy((rotation.x, rotation.y, rotation.z, rotation.w))],
            'age_s':         round(age_s, 3),
            'meaning':       f'the pose of {transform.child_frame_id} in {transform.header.frame_id}',
        }
