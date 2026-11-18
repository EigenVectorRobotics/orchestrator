"""press_button refuses to run before any set_press_offset: move_to_pose may hold an offset from before this executor."""

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from skill_registry import Registry

from orchestrator.tree.engine import Status

from g1_skills.check_reach import skill as check_reach
from g1_skills.lib.press import press_state
from g1_skills.lib.tracking import tracking
from g1_skills.press_button.skill import PressButton
from g1_skills.set_press_offset.skill import SetPressOffset

from fakes import FakeCtx


CONTRACTS = Path(__file__).resolve().parents[1] / 'g1_skills'


def test_a_press_before_any_set_press_offset_fails_without_moving():
    press = PressButton(Registry.from_directory(CONTRACTS).get('press_button'), FakeCtx())
    press.setup()

    handle = press.before({'target_description': 'up button'})

    assert handle.poll() == Status.FAILURE
    assert 'no set_press_offset since the executor started' in handle.message


def test_set_press_offset_gives_the_press_its_offset():
    registry = Registry.from_directory(CONTRACTS)
    ctx      = FakeCtx()
    press    = PressButton(registry.get('press_button'), ctx)
    press.setup()

    assert SetPressOffset(registry.get('set_press_offset'), ctx).before({'x': 0.003, 'y': 0.0, 'z': -0.001}) is None
    assert press_state(ctx).offset == (0.003, 0.0, -0.001)
    assert press.before({'target_description': 'up button'}) is None


def test_a_reach_check_before_any_set_press_offset_uses_offset_0(monkeypatch):
    """A look runs check_reach before the press slot's first set_press_offset: the target goes to ik_server as it is"""
    import time

    from geometry_msgs.msg import PoseStamped

    class Request(SimpleNamespace):
        pass

    monkeypatch.setattr(check_reach, 'get_service', lambda name: SimpleNamespace(Request=Request))
    ctx   = FakeCtx()
    skill = check_reach.CheckReach(Registry.from_directory(CONTRACTS).get('check_reach'), ctx)
    tracking(ctx).follow_pose = lambda topic: None
    skill.setup()
    pose  = PoseStamped()
    pose.pose.position.x, pose.pose.position.y, pose.pose.position.z = 0.4, -0.1, 1.0
    pose.pose.orientation.w = 1.0
    skill.tracking.poses[skill.pose_topic] = (time.monotonic(), pose)

    handle = skill.start({'left_arm': False})

    assert handle.offset == (0.0, 0.0, 0.0)
    position = handle.request.target_pose.pose.position
    assert (position.x, position.y, position.z) == pytest.approx((0.4, -0.1, 1.0))
