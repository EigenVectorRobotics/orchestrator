"""TopicWatch: the last N messages must match for Watch.consecutive."""

from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from skill_registry.contract import Watch

from orchestrator.behavior_tree import Status
from orchestrator.ros_skill_runner import RosSkillRunner, TopicWatch


NODE = SimpleNamespace(create_subscription=lambda *args: None)
SPEC = Watch(topic='/door_state', ros_type='std_msgs/msg/String', equals='OPEN', consecutive=3)


def _watch(values: list[str]) -> TopicWatch:
    watch = TopicWatch(NODE, SPEC.topic, SPEC.ros_type, False)

    for value in values:
        watch._on_message(SimpleNamespace(data=value))

    return watch


def test_three_open_in_a_row_match():
    assert _watch(['CLOSED', 'OPEN', 'OPEN', 'OPEN']).matches(SPEC)


def test_one_closed_in_the_last_three_does_not_match():
    assert not _watch(['OPEN', 'OPEN', 'CLOSED', 'OPEN', 'OPEN']).matches(SPEC)


def test_fewer_messages_than_consecutive_do_not_match():
    assert not _watch(['OPEN', 'OPEN']).matches(SPEC)


def test_consecutive_one_reads_the_latest_only():
    assert _watch(['CLOSED', 'OPEN']).matches(Watch(topic='/door_state', ros_type='std_msgs/msg/String', equals='OPEN'))


def test_equals_input_compares_with_the_tree_input():
    runner = SimpleNamespace(_watch=lambda spec: watch)
    spec   = Watch(topic='/slam_elevatorlio/floor', ros_type='std_msgs/msg/Int32', equals_input='floor', latched=True)
    watch  = TopicWatch(NODE, spec.topic, spec.ros_type, True)
    watch._on_message(SimpleNamespace(data=2))

    assert RosSkillRunner._check_watch(runner, spec, {'floor': 2}).status == Status.SUCCESS
    assert RosSkillRunner._check_watch(runner, spec, {'floor': 1}).status == Status.FAILURE
