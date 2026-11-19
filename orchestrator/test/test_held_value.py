import pytest

pytest.importorskip('rclpy')

from skill_registry.contract import HeldValue

from skill_registry.ros_messages import read_field
from orchestrator.ros_skill_runner import HoldTimer


def test_a_value_must_hold_without_a_break():
    timer = HoldTimer(HeldValue(field='state', equals='blocked', hold_s=10))

    timer.update('blocked', 0.0)
    assert not timer.expired(9.9)

    timer.update('routing', 5.0)                        # cleared: the count starts again
    timer.update('blocked', 6.0)
    assert not timer.expired(15.9)
    assert timer.expired(16.0)


def test_read_field_follows_a_dotted_path():
    class Feedback:
        state = 'blocked'
        nested = {'inner': 3}

    assert read_field(Feedback(), 'state') == 'blocked'
    assert read_field(Feedback(), 'nested.inner') == 3
