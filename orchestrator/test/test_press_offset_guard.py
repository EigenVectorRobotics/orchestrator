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
