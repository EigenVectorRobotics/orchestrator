"""Print the newest colour frame as a small JPEG with the EdgeTAM mask outlines (2 Hz), as JSON lines; it runs on the camera host, so the raw image stays there."""

import base64
import json
import time

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import deserialize_message
from sensor_msgs.msg import CompressedImage, Image
from std_msgs.msg import Header
from visualization_msgs.msg import ImageMarker


PERIOD_S     = 0.5
CAMERA       = '/camera/camera/color/image_raw'   # track_3d's colour input; its /compressed is used when it has a publisher
MASKS        = '/track_3d/mask_outlines'           # EdgeTAM mask outlines, LINE_LIST in image pixels, one colour per track
CAMERA_WIDTH = 640
JPEG_QUALITY = 70
ANY          = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)


def seconds(
    header: Header,
) -> float:
    """The stamp of a header in seconds."""
    return header.stamp.sec + header.stamp.nanosec * 1e-9


class CameraEcho(Node):
    """Keep the newest frame and mask outlines as bytes; decode and print them on a timer, so only 2 frames a second are decoded."""

    def __init__(self) -> None:
        """Subscribe to the mask outlines now and to the camera once it has a publisher."""
        super().__init__('run_viewer_camera_echo')
        self.image  = None
        self.sent   = None
        self.masks  = None
        self.camera = None
        self.kind   = None
        self.create_subscription(ImageMarker, MASKS, self.on_masks, ANY, raw=True)
        self.create_timer(PERIOD_S, self.emit)
        self.create_timer(2.0, self.choose_camera)

    def choose_camera(self) -> None:
        """Subscribe once to the compressed colour image if it has a publisher, else to the raw one: one stream, not both."""
        if self.camera is not None:
            return
        for topic, kind in ((CAMERA + '/compressed', CompressedImage), (CAMERA, Image)):
            found = self.get_publishers_info_by_topic(topic)
            if found:                         # its reliability: a big frame sent best effort often never arrives whole
                self.camera, self.kind = topic, kind
                qos = QoSProfile(depth=1, reliability=found[0].qos_profile.reliability, durability=DurabilityPolicy.VOLATILE)
                self.create_subscription(kind, topic, self.on_image, qos, raw=True)
                return

    def on_image(
        self,
        message: bytes,
    ) -> None:
        """The latest colour frame, serialized."""
        self.image = message

    def on_masks(
        self,
        message: bytes,
    ) -> None:
        """The latest mask outlines, serialized."""
        self.masks = message

    def emit(self) -> None:
        """Print the newest frame as a small JPEG with the mask outlines scaled to it."""
        if self.image is None or self.image is self.sent:
            return
        self.sent = self.image
        message = deserialize_message(self.image, self.kind)
        masks = deserialize_message(self.masks, ImageMarker) if self.masks is not None else None
        if isinstance(message, CompressedImage):
            frame = cv2.imdecode(np.frombuffer(message.data, np.uint8), cv2.IMREAD_COLOR)
        else:
            frame = np.frombuffer(message.data, np.uint8).reshape(message.height, message.step)[:, :message.width * 3].reshape(message.height, message.width, 3)
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR) if message.encoding == 'rgb8' else frame
        scale = CAMERA_WIDTH / frame.shape[1]
        small = cv2.resize(frame, (CAMERA_WIDTH, round(frame.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        jpeg = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])[1]
        segments, colors, lag = [], [], None
        if masks is not None:
            lag = round(seconds(message.header) - seconds(masks.header), 2)
            points = masks.points
            for i in range(0, len(points) - 1, 2):
                segments.append([round(points[i].x * scale, 1), round(points[i].y * scale, 1), round(points[i + 1].x * scale, 1), round(points[i + 1].y * scale, 1)])
                c = masks.outline_colors[i] if i < len(masks.outline_colors) else None
                colors.append('#%02x%02x%02x' % (int(c.r * 255), int(c.g * 255), int(c.b * 255)) if c else '#00ff66')
        print(json.dumps({
            'name': 'camera', 'time': time.time(), 'topic': self.camera, 'w': small.shape[1], 'h': small.shape[0],
            'jpeg': base64.b64encode(jpeg.tobytes()).decode(), 'segments': segments, 'colors': colors, 'mask_lag_s': lag,
        }), flush=True)


def main() -> None:
    """Spin until killed."""
    rclpy.init()
    rclpy.spin(CameraEcho())


if __name__ == '__main__':
    main()
