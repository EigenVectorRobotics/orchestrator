"""button_in_view: seed the tracker, then judge the first box and 3D pose after the seed."""

import time

from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from skill_registry import parse_contract

from orchestrator.behavior_tree import Status
from orchestrator.ros_skill_runner import TopicWatch, _InViewHandle


CONTRACT = parse_contract({
    'name': 'button_in_view', 'kind': 'action', 'device': 'orin', 'timeout_s': 15,
    'interface': {
        'transport': 'in_view', 'name': '/keyboard_input', 'ros_type': 'std_msgs/msg/String',
        'success_when': {'topic': '/track_3d/is_tracking', 'ros_type': 'std_msgs/msg/Bool', 'field': 'data', 'equals': True},
        'target': {'box_topic': '/track_3d/detections_2d', 'pose_topic': '/track_3d/selected_normal_pose', 'frame': 'pelvis'},
        'limits': {'max_wait_s': 0.2, 'margin_frac': 0.05, 'left_arm_min_y_m': 0.10},
    },
    'inputs': [{'name': 'query', 'type': 'string'}],
})
POSE = SimpleNamespace(pose=SimpleNamespace(position=SimpleNamespace(x=0.1, y=0.2, z=0.5)), header=SimpleNamespace(frame_id='camera'))


class FakePublisher:
    def __init__(self):
        self.sent = []

    def get_subscription_count(self):
        return 1

    def publish(self, message):
        self.sent.append(message.data)


def _runner(in_pelvis=(0.42, -0.05, 0.25)):
    watch = TopicWatch(SimpleNamespace(create_subscription=lambda *args: None), '/track_3d/is_tracking', 'std_msgs/msg/Bool', False)
    publisher = FakePublisher()

    return SimpleNamespace(_publisher=lambda *args: publisher, _watch=lambda spec: watch, server_wait_s=1.0, _box_seen=None,
                           _targets={}, _image_size=(1280, 720), watch=watch, publisher=publisher,
                           _pose_in=lambda message, frame: (in_pelvis, (0.0, 0.0, 0.0, 1.0)))


def _seeded(runner):
    handle = _InViewHandle(runner, CONTRACT, {'query': 'down button'})
    assert handle.poll() == Status.RUNNING

    return handle


def _track(runner, box):
    now = time.monotonic() + 0.01
    runner.watch._on_message(SimpleNamespace(data=True))
    runner.watch.receive_time = now
    runner._box_seen = (now, box)
    runner._targets['/track_3d/selected_normal_pose'] = (now, POSE)


def test_it_seeds_then_passes_a_box_inside_the_image():
    runner = _runner()
    handle = _seeded(runner)
    _track(runner, (600, 400, 680, 480))

    assert runner.publisher.sent == ['down button']
    assert handle.poll() == Status.SUCCESS
    assert handle.outputs['pose_camera'] == [0.1, 0.2, 0.5]
    assert handle.outputs['left_arm'] is False and 'right arm' in handle.message


def test_a_button_well_to_the_left_takes_the_left_arm():
    runner = _runner(in_pelvis=(0.40, 0.18, 0.25))
    handle = _seeded(runner)
    _track(runner, (600, 400, 680, 480))

    assert handle.poll() == Status.SUCCESS
    assert handle.outputs['left_arm'] is True and handle.outputs['position_pelvis'] == [0.4, 0.18, 0.25]


def test_a_box_at_the_bottom_edge_fails():
    runner = _runner()
    handle = _seeded(runner)
    _track(runner, (600, 680, 680, 719))

    assert handle.poll() == Status.FAILURE
    assert 'bottom' in handle.message and handle.outputs['left_arm'] is False


def test_a_box_from_before_the_seed_does_not_count():
    runner = _runner()
    runner._box_seen = (time.monotonic() - 5.0, (600, 400, 680, 480))
    handle = _seeded(runner)
    time.sleep(0.25)

    assert handle.poll() == Status.FAILURE
    assert 'no track' in handle.message and 'no 2D box' in handle.message and 'no 3D pose' in handle.message
    assert handle.outputs == {'left_arm': False}


def test_tracking_with_a_box_but_no_pose_names_the_missing_depth():
    runner = _runner()
    handle = _seeded(runner)
    _track(runner, (600, 400, 680, 480))
    del runner._targets['/track_3d/selected_normal_pose']
    time.sleep(0.25)

    assert handle.poll() == Status.FAILURE
    assert handle.message.endswith('no 3D pose: too close for depth, or no depth points; right arm')
