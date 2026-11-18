from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from builtin_interfaces.msg import Time as TimeMsg
from rclpy.clock import ClockType
from rclpy.time import Time

from orchestrator.sensing import Tf, TfError


class FakeBuffer:
    def __init__(self):
        self.asked = []

    def can_transform(self, target, source, time):
        return True

    def lookup_transform(self, target, source, time):
        self.asked.append((target, source))
        return SimpleNamespace(header=SimpleNamespace(stamp=TimeMsg(), frame_id=target), child_frame_id=source, transform=SimpleNamespace(
            translation=SimpleNamespace(x=1.0, y=2.0, z=0.0), rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0)))


def test_tf_goes_through_the_given_buffer():
    buffer = FakeBuffer()
    node   = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: Time(clock_type=ClockType.ROS_TIME)))
    tf     = Tf(node)
    tf._buffer = buffer                             # the one buffer the executor's skills and agent share

    found = tf.numbers(tf.lookup('map', 'pelvis', timeout_s=1.0))

    assert buffer.asked == [('map', 'pelvis')]
    assert found['translation_m'] == [1.0, 2.0, 0.0] and found['rpy_rad'] == [0.0, 0.0, 0.0]
    assert found['meaning'] == 'the pose of pelvis in map'


def test_a_missing_transform_is_a_tf_error_and_no_wait_by_default():
    class Missing(FakeBuffer):
        def can_transform(self, target, source, time):
            raise AssertionError('a lookup with no timeout never polls')

        def lookup_transform(self, target, source, time):
            raise LookupError('frame pelvis does not exist')

    tf         = Tf(SimpleNamespace())
    tf._buffer = Missing()

    with pytest.raises(TfError, match='no transform map <- pelvis'):
        tf.lookup('map', 'pelvis')
