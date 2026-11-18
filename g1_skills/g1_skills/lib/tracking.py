import time
import threading

from geometry_msgs.msg import PoseStamped

from orchestrator.common.topics import COMMAND_QOS


class BoxBuffer:
    """The target button's box per camera frame: (stamp, (x_min, y_min, x_max, y_max) in pixels)"""

    def __init__(self, keep_s: float = 8.0):
        self.keep_s = keep_s
        self._boxes = []
        self._lock  = threading.Lock()

    def add(self, t: float, box: tuple):
        with self._lock:
            self._boxes.append((t, box))
            self._boxes = [entry for entry in self._boxes if entry[0] >= t - self.keep_s]

    def at(self, t: float, max_age_s: float):
        """The box of the frame stamped t, or the latest before it within max_age_s"""
        with self._lock:
            earlier = [entry for entry in self._boxes if t - max_age_s <= entry[0] <= t + 1e-3]

        return earlier[-1][1] if earlier else None


class Tracking:
    """What track_anything publishes, kept for every skill that reads it: the target's 2D box per frame (by its camera
    stamp, for crops) and as last received (for freshness), and the latest 3D pose on each pose topic"""

    def __init__(self, ctx):
        self.ctx        = ctx
        self.boxes      = BoxBuffer()
        self.box_seen   = None          # (monotonic receive time, box) of the latest target box
        self.target_id  = None
        self.poses      = {}            # pose topic -> (monotonic receive time, PoseStamped)
        self._box_topics  = set()
        self._pose_topics = set()

    def follow_boxes(self, box_topic: str, target_topic: str = ''):
        """track_anything's 2D boxes (stamped as their camera frame) and the id of the target it selected"""
        if box_topic in self._box_topics:
            return

        try:
            from vision_msgs.msg import Detection2DArray, Detection3D
        except ImportError as error:
            self.ctx.logger.warn(f'no vision_msgs ({error}): no button boxes, so press frames are kept without crops')
            return

        self._box_topics.add(box_topic)
        self.ctx.node.create_subscription(Detection2DArray, box_topic, self._on_boxes, COMMAND_QOS)

        if target_topic:
            self.ctx.node.create_subscription(Detection3D, target_topic, lambda message: setattr(self, 'target_id', message.id), COMMAND_QOS)

    def follow_pose(self, topic: str):
        if topic in self._pose_topics:
            return

        self._pose_topics.add(topic)
        self.ctx.node.create_subscription(PoseStamped, topic, lambda message: self.poses.__setitem__(topic, (time.monotonic(), message)), COMMAND_QOS)
        self.ctx.tf.buffer()                            # listen from the start, so TF is there at the first check

    def _on_boxes(self, message):
        if not message.detections:
            return

        detection = next((item for item in message.detections if item.id == self.target_id), message.detections[0])
        centre, box = detection.bbox.center.position, detection.bbox
        stamp       = message.header.stamp.sec + message.header.stamp.nanosec * 1e-9
        corners     = (centre.x - box.size_x / 2, centre.y - box.size_y / 2, centre.x + box.size_x / 2, centre.y + box.size_y / 2)

        self.boxes.add(stamp or self.ctx.now(), corners)
        self.box_seen = (time.monotonic(), corners)     # by receive time: the Orin's stamps need not match this clock


def tracking(ctx) -> Tracking:
    """The executor's one Tracking"""
    return ctx.shared('tracking', lambda: Tracking(ctx))
