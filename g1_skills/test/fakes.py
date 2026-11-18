from types import SimpleNamespace

from orchestrator.skills.transports import TopicWatch


NODE = SimpleNamespace(create_subscription=lambda *args: None, create_timer=lambda *args: None)


class FakePublisher:
    def __init__(self):
        self.sent = []

    def get_subscription_count(self):
        return 1

    def publish(self, message):
        self.sent.append(message.data)


class FakeTf:
    def __init__(self, in_frame=(0.42, -0.05, 0.25)):
        self.in_frame = in_frame

    def buffer(self):
        return None

    def pose_in(self, message, frame):
        return self.in_frame, (0.0, 0.0, 0.0, 1.0)


class FakeCtx:
    """What a skill sees of the executor, without ROS: shared state, cached watches and publishers, a camera of one size"""

    def __init__(self, in_frame=(0.42, -0.05, 0.25), artifacts_dir: str = '/tmp'):
        self.node          = NODE
        self.server_wait_s = 1.0
        self.artifacts_dir = artifacts_dir
        self.camera        = SimpleNamespace(jpeg=None, size=(1280, 720), frames=None, topic='/camera', frame_id=None, k=None)
        self.tf            = FakeTf(in_frame)
        self.logger        = SimpleNamespace(warn=lambda text: None)
        self.publishers    = {}
        self.watches       = {}
        self.services      = {}
        self._shared       = {}

    def now(self) -> float:
        return 0.0

    def shared(self, key, factory):
        if key not in self._shared:
            self._shared[key] = factory()

        return self._shared[key]

    def watch(self, spec):
        if spec.topic not in self.watches:
            self.watches[spec.topic] = TopicWatch(NODE, spec.topic, spec.ros_type, spec.latched)

        return self.watches[spec.topic]

    def publisher(self, name, ros_type):
        return self.publishers.setdefault(name, FakePublisher())

    def service_client(self, name, service_type):
        return self.services.get(name)


def press_ctx(press: dict | None):
    """What an agent skill sees of the executor when the last press gave press: no TF, no camera"""
    state = SimpleNamespace(evidence=lambda wait_s=8.0: press, offset=None, last_capture=None)

    return SimpleNamespace(tf=None, camera=None, logger=SimpleNamespace(warn=lambda text: None), artifacts_dir='/tmp',
                           shared=lambda key, factory: state)
