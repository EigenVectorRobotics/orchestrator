"""TopicWatch: the last N messages must match for Watch.consecutive."""

from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from skill_registry.contract import Watch

from orchestrator.tree.engine import Status
from orchestrator.skills.transports import ServiceHandle
from orchestrator.skills.transports import check_watch
from orchestrator.skills.transports import TopicWatch


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
    ctx    = SimpleNamespace(watch=lambda spec: watch)
    spec   = Watch(topic='/slam_elevatorlio/floor', ros_type='std_msgs/msg/Int32', equals_input='floor', latched=True)
    watch  = TopicWatch(NODE, spec.topic, spec.ros_type, True)
    watch._on_message(SimpleNamespace(data=2))

    assert check_watch(spec, {'floor': 2}, ctx).status == Status.SUCCESS
    assert check_watch(spec, {'floor': 1}, ctx).status == Status.FAILURE


def test_max_age_fails_a_watch_whose_latest_message_is_old():
    ctx    = SimpleNamespace(watch=lambda spec: watch)
    spec   = Watch(topic='/body_ekf/torso_pose', ros_type='geometry_msgs/msg/PoseStamped', field='header.frame_id',
                   equals='foot_world', max_age_s=0.5)
    watch  = TopicWatch(NODE, spec.topic, spec.ros_type, False)
    watch._on_message(SimpleNamespace(header=SimpleNamespace(frame_id='foot_world')))

    assert check_watch(spec, {}, ctx).status == Status.SUCCESS

    watch.receive_time -= 1.0                       # no pose for a second: body_ekf is recalibrating

    result = check_watch(spec, {}, ctx)
    assert result.status == Status.FAILURE and 'for 1.0 s' in result.message


def test_a_service_with_a_watch_ends_on_a_message_after_its_response():
    spec     = Watch(topic='/body_ekf/torso_pose', ros_type='geometry_msgs/msg/PoseStamped', field='header.frame_id',
                     equals='foot_world')
    watch    = TopicWatch(NODE, spec.topic, spec.ros_type, False)
    pose     = SimpleNamespace(header=SimpleNamespace(frame_id='foot_world'))
    response = SimpleNamespace(success=True, message='collecting still period')
    future   = SimpleNamespace(done=lambda: True, result=lambda: response)
    client   = SimpleNamespace(service_is_ready=lambda: True, call_async=lambda request: future)
    contract = SimpleNamespace(interface=SimpleNamespace(name='/body_ekf/reset', success_when=spec), outputs=[])
    handle   = ServiceHandle(client, None, contract, 1.0, watch)

    watch._on_message(pose)                         # a pose from before the reset does not end it
    assert handle.poll() == Status.RUNNING
    assert handle.poll() == Status.RUNNING

    watch._on_message(pose)                         # the first pose after the reset: body_ekf has recalibrated
    assert handle.poll() == Status.SUCCESS and handle.message == 'collecting still period'
