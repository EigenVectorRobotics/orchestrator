"""button_in_view: seed the tracker, then judge the first box and 3D pose after the seed."""

import time

from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from skill_registry import parse_contract

from orchestrator.tree.engine import Status

from g1_skills.button_in_view.skill import ButtonInView
from g1_skills.lib.tracking import tracking

from fakes import FakeCtx


CONTRACT = parse_contract({
    'name': 'button_in_view', 'kind': 'action', 'device': 'orin', 'timeout_s': 15,
    'interface': {
        'transport': 'python', 'name': '/keyboard_input', 'ros_type': 'std_msgs/msg/String',
        'success_when': {'topic': '/track_3d/is_tracking', 'ros_type': 'std_msgs/msg/Bool', 'field': 'data', 'equals': True},
    },
    'config': {
        'target': {'box_topic': '/track_3d/detections_2d', 'pose_topic': '/track_3d/selected_normal_pose', 'frame': 'pelvis'},
        'limits': {'max_wait_s': 0.2, 'margin_frac': 0.05, 'left_arm_max_x_frac': 0.4},
    },
    'inputs': [{'name': 'query', 'type': 'string'}],
})
POSE = SimpleNamespace(pose=SimpleNamespace(position=SimpleNamespace(x=0.1, y=0.2, z=0.5)), header=SimpleNamespace(frame_id='camera'))


def _skill(in_pelvis=(0.42, -0.05, 0.25)) -> ButtonInView:
    skill = ButtonInView(CONTRACT, FakeCtx(in_pelvis))
    tracker = tracking(skill.ctx)
    tracker.follow_boxes = tracker.follow_pose = lambda *args: None     # no ROS here: the test feeds the tracking itself
    skill.setup()

    return skill


def _seeded(skill):
    handle = skill.start({'query': 'down button'})
    assert handle.poll() == Status.RUNNING

    return handle


def _track(skill, box):
    now   = time.monotonic() + 0.01
    watch = skill.ctx.watches['/track_3d/is_tracking']
    watch._on_message(SimpleNamespace(data=True))
    watch.receive_time = now
    skill.tracking.box_seen = (now, box)
    skill.tracking.poses['/track_3d/selected_normal_pose'] = (now, POSE)


def test_it_seeds_then_passes_a_box_inside_the_image():
    skill  = _skill()
    handle = _seeded(skill)
    _track(skill, (600, 400, 680, 480))

    assert skill.ctx.publishers['/keyboard_input'].sent == ['down button']
    assert handle.poll() == Status.SUCCESS
    assert handle.outputs['pose_camera'] == [0.1, 0.2, 0.5]
    assert handle.outputs['left_arm'] is False and 'right arm' in handle.message


def test_a_button_in_the_left_of_the_image_takes_the_left_arm():
    skill  = _skill(in_pelvis=(0.40, 0.18, 0.25))
    handle = _seeded(skill)
    _track(skill, (300, 400, 380, 480))

    assert handle.poll() == Status.SUCCESS
    assert handle.outputs['left_arm'] is True and handle.outputs['position_pelvis'] == [0.4, 0.18, 0.25]


def test_a_button_left_of_the_pelvis_but_right_in_the_image_takes_the_right_arm():
    skill  = _skill(in_pelvis=(0.52, 0.02, 0.28))
    handle = _seeded(skill)
    _track(skill, (690, 410, 740, 460))

    assert handle.poll() == Status.SUCCESS
    assert handle.outputs['left_arm'] is False and 'right arm' in handle.message


def test_a_box_at_the_bottom_edge_fails():
    skill  = _skill()
    handle = _seeded(skill)
    _track(skill, (600, 680, 680, 719))

    assert handle.poll() == Status.FAILURE
    assert 'bottom' in handle.message and handle.outputs['left_arm'] is False


def test_a_box_from_before_the_seed_does_not_count():
    skill  = _skill()
    skill.tracking.box_seen = (time.monotonic() - 5.0, (600, 400, 680, 480))
    handle = _seeded(skill)
    time.sleep(0.25)

    assert handle.poll() == Status.FAILURE
    assert 'no track' in handle.message and 'no 2D box' in handle.message and 'no 3D pose' in handle.message
    assert handle.outputs == {'left_arm': False}


def test_tracking_with_a_box_but_no_pose_names_the_missing_depth():
    skill  = _skill()
    handle = _seeded(skill)
    _track(skill, (600, 400, 680, 480))
    del skill.tracking.poses['/track_3d/selected_normal_pose']
    time.sleep(0.25)

    assert handle.poll() == Status.FAILURE
    assert handle.message.endswith('no 3D pose: too close for depth, or no depth points; right arm')
