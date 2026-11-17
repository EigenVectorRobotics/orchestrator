"""press_button refuses to run before any set_press_offset: move_to_pose may hold an offset from before this executor."""

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from skill_registry import Registry

from orchestrator.behavior_tree import Status
from orchestrator.ros_skill_runner import RosSkillRunner


CONTRACTS = Path(__file__).resolve().parents[2] / 'skill_registry' / 'contracts'


def test_a_press_before_any_set_press_offset_fails_without_moving():
    press  = Registry.from_directory(CONTRACTS).get('press_button')
    runner = SimpleNamespace(press_offset=None)

    handle = RosSkillRunner.start(runner, press, {'target_description': 'up button'})

    assert handle.poll() == Status.FAILURE
    assert 'no set_press_offset since the executor started' in handle.message


def test_a_reach_check_before_any_set_press_offset_uses_offset_0(monkeypatch):
    """A look runs check_reach before the press slot's first set_press_offset: the target goes to ik_server as it is"""
    import time

    from geometry_msgs.msg import PoseStamped

    from orchestrator import ros_skill_runner

    class Request(SimpleNamespace):
        pass

    monkeypatch.setattr(ros_skill_runner, 'get_service', lambda name: SimpleNamespace(Request=Request))
    check  = Registry.from_directory(CONTRACTS).get('check_reach')
    pose   = PoseStamped()
    pose.pose.position.x, pose.pose.position.y, pose.pose.position.z = 0.4, -0.1, 1.0
    pose.pose.orientation.w = 1.0
    runner = SimpleNamespace(press_offset=None, server_wait_s=1.0, _service_client=lambda *args: None,
                             _targets={check.interface.target['pose_topic']: (time.monotonic(), pose)})

    handle = ros_skill_runner._ReachHandle(runner, check, {'left_arm': False})

    assert handle.offset == (0.0, 0.0, 0.0)
    position = handle.request.target_pose.pose.position
    assert (position.x, position.y, position.z) == pytest.approx((0.4, -0.1, 1.0))
