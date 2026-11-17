"""RosReader reads TF through the buffer it is given (the skill runner's), so the executor keeps one TF listener."""

from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from builtin_interfaces.msg import Time as TimeMsg
from rclpy.clock import ClockType
from rclpy.time import Time

from orchestrator.agent_ros import RosReader


class FakeBuffer:
    def __init__(self):
        self.asked = []

    def can_transform(self, target, source, time):
        return True

    def lookup_transform(self, target, source, time):
        self.asked.append((target, source))
        return SimpleNamespace(header=SimpleNamespace(stamp=TimeMsg()), transform=SimpleNamespace(
            translation=SimpleNamespace(x=1.0, y=2.0, z=0.0), rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0)))


def test_tf_goes_through_the_given_buffer():
    buffer = FakeBuffer()
    node   = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: Time(clock_type=ClockType.ROS_TIME)))
    reader = RosReader(node, lambda: None, lambda: buffer)

    found = reader.tf('map', 'pelvis')

    assert buffer.asked == [('map', 'pelvis')]
    assert found['translation_m'] == [1.0, 2.0, 0.0] and found['rpy_rad'] == [0.0, 0.0, 0.0]
