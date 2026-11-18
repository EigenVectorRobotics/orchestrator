from rclpy.node import Node
from sensor_msgs.msg import Image

from ..common.images import ImageError, image_to_jpeg
from ..common.topics import SENSOR_QOS
from .frames import FrameBuffer


class Camera:
    """The head camera: its latest frame as JPEG, its size, and the last seconds of frames by their stamps"""

    def __init__(self, node: Node, topic: str):
        self.node   = node
        self.topic  = topic
        self.jpeg   = None
        self.size   = None              # (width, height) of the last frame
        self.frames = FrameBuffer()

        node.create_subscription(Image, topic, self._on_image, SENSOR_QOS)

    def _on_image(self, message: Image):
        """Raw frames from the RealSense, encoded here: the Orin publishes no compressed transport"""
        try:
            jpeg = image_to_jpeg(message)
        except ImageError as error:
            self.node.get_logger().warn(f'camera frame dropped: {error}', throttle_duration_sec=10.0)
            return

        stamp     = message.header.stamp.sec + message.header.stamp.nanosec * 1e-9
        self.jpeg = jpeg
        self.size = (message.width, message.height)
        self.frames.add(stamp or self.node.get_clock().now().nanoseconds * 1e-9, jpeg)
