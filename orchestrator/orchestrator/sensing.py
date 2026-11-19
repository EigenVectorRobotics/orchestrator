"""Streams that skills share: the head camera, for now. The runner reads it for its VLM skills; it does not subscribe
itself"""

from rclpy.node import Node
from sensor_msgs.msg import Image

from .qos import SENSOR_QOS
from .images import Camera


class CameraStream:
    """The last frame of one camera, as full-size JPEG. The Orin's RealSense publishes only raw frames, so a frame is
    encoded here, and only when it is read (images.Camera)"""

    def __init__(self, node: Node, topic: str):
        self.topic   = topic
        self._camera = Camera(topic)

        node.create_subscription(Image, topic, self._camera.put, SENSOR_QOS)

    def has_frame(self) -> bool:
        """A frame has come, without encoding it"""
        return self._camera.state()['have']

    def latest(self) -> bytes | None:
        """The last frame as JPEG; None before the first frame, or when it cannot be encoded (see error)"""
        frame = self._camera.frame()

        return frame[0] if frame else None

    @property
    def error(self) -> str:
        return self._camera.error
