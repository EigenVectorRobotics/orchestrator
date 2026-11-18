import io

import numpy as np
import pytest

from PIL import Image
from skill_registry import Registry

from pathlib import Path

from orchestrator.sensing.frames import FrameBuffer

from g1_skills.lib.boxes import BoxBuffer
from g1_skills.press_button.capture import CaptureConfig, PhaseTracker, PressCapture, crop_around


CONTRACTS = Path(__file__).resolve().parents[1] / 'g1_skills'
GOAL      = (0.62, -0.18, 1.05)
PRESS     = [(0.0, -0.40), (0.4, -0.10), (0.5, -0.10), (0.7, 0.0), (0.75, 0.0), (1.0, -0.10)]   # (phase, depth)
DURATION  = 3.0


def _jpeg(colour, size=(64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, colour).save(buffer, format='JPEG')
    return buffer.getvalue()


def _play(capture: PressCapture, buffer: FrameBuffer, profile=PRESS, fps: float = 6.0, after_s: float = 1.0):
    """The press at 100 Hz poses and fps frames; frame k is coloured by k so the choice can be read back"""
    capture.on_goal(GOAL, (0, 0, 0, 1))
    frame = 0

    for step in range(int((DURATION + after_s) * 100) + 1):
        t = step / 100

        while frame / fps <= t:
            buffer.add(frame / fps, _jpeg((min(255, frame), 0, 0)))
            frame += 1

        if t <= DURATION:
            depth = float(np.interp(t / DURATION, [p[0] for p in profile], [p[1] for p in profile]))
            capture.on_pose(t, (GOAL[0] + depth, GOAL[1], GOAL[2]))

        if abs(t - DURATION) < 1e-9:
            capture.on_action_end(t)


def test_the_tracker_finds_the_pregrasp_the_deepest_point_and_the_release():
    tracker = PhaseTracker(CaptureConfig())
    tracker.set_goal(GOAL, (0, 0, 0, 1))

    for step in range(301):
        t     = step / 100
        depth = float(np.interp(t / DURATION, [p[0] for p in PRESS], [p[1] for p in PRESS]))
        tracker.update(t, (GOAL[0] + depth, GOAL[1], GOAL[2]))

    assert 0.37 * DURATION < tracker.pregrasp_t <= 0.4 * DURATION                   # within 2 cm of the pre-grasp
    assert tracker.deepest_t == pytest.approx(0.7 * DURATION, abs=0.02)
    assert 0.75 * DURATION < tracker.release_t < 0.75 * DURATION + 0.1


def test_the_capture_chooses_the_three_frames_at_6_fps():
    buffer  = FrameBuffer()
    capture = PressCapture(CaptureConfig(), buffer, left_arm=False)
    _play(capture, buffer)

    assert capture.ready(DURATION + 1.0)
    times = {name: t for name, (t, _) in capture.frames.items()}
    tracker = capture.tracker

    assert set(times) == {'baseline', 'press', 'release'}
    assert tracker.pregrasp_t <= times['baseline'] < tracker.pregrasp_t + 1 / 6
    assert tracker.deepest_t + 0.005 <= times['press'] < tracker.deepest_t + 0.005 + 1 / 6
    assert tracker.release_t + 0.5 <= times['release'] < tracker.release_t + 0.5 + 1 / 6


def test_each_frame_is_written_as_soon_as_it_is_chosen(tmp_path):
    buffer  = FrameBuffer()
    capture = PressCapture(CaptureConfig(), buffer, left_arm=False, directory=str(tmp_path))
    pushed  = 0.6 * DURATION                        # before the deepest point, only the baseline can be chosen
    _play(capture, buffer, profile=[point for point in PRESS if point[0] <= 0.6] + [(1.0, -0.05)], after_s=0.0)

    written = sorted(path.name for path in tmp_path.iterdir())
    assert capture.tracker.pregrasp_t < pushed and capture.tracker.deepest_t is None
    assert len(written) == 1 and written[0].endswith('_baseline.jpg')
    assert capture.saved == {'baseline': str(tmp_path / written[0])}

    _play(capture, buffer)
    assert {Path(path).name.rsplit('_', 1)[1] for path in capture.save(str(tmp_path)).values()} == {'baseline.jpg', 'press.jpg', 'release.jpg'}
    assert not [path for path in tmp_path.iterdir() if path.name.endswith('.part')]


def test_a_press_that_ends_at_the_button_still_gets_its_frames():
    # move_to_pose ends a press with no target update without a retreat: the arm stays on the button
    buffer  = FrameBuffer()
    capture = PressCapture(CaptureConfig(), buffer, left_arm=False)
    _play(capture, buffer, profile=[(0.0, -0.40), (0.4, -0.10), (0.5, -0.10), (0.9, 0.0), (1.0, 0.0)])

    assert capture.tracker.deepest_t == pytest.approx(0.9 * DURATION, abs=0.02)
    assert capture.tracker.release_t == pytest.approx(DURATION)
    assert capture.ready(DURATION + 1.0) and set(capture.frames) == {'baseline', 'press', 'release'}


def test_a_deeper_press_offset_still_finds_the_pregrasp():
    # move_to_pose puts the pre-grasp 0.10 m in front of the offset goal: 20 mm deeper, it sits at -0.08 m from the goal
    # The hand comes in from the side and lines up only at the pre-grasp, as on the robot
    def path(t):
        if t < 1.0:
            return (-0.08, 0.20)
        if t < 1.5:
            return (-0.08, 0.0)
        if t < 2.1:
            return (-0.08 + 0.10 * (t - 1.5) / 0.6, 0.0)
        return (0.02 - 0.10 * min(1.0, (t - 2.1) / 0.6), 0.0)

    captures = {'told': PressCapture(CaptureConfig(), FrameBuffer(), left_arm=False, press_offset=(0.02, 0.0, 0.0)),
                'untold': PressCapture(CaptureConfig(), FrameBuffer(), left_arm=False)}

    for capture in captures.values():
        capture.on_goal(GOAL, (0, 0, 0, 1))
        for step in range(301):
            t = step / 100
            if step % 17 == 0:
                capture.buffer.add(t, _jpeg((step % 255, 0, 0)))
            depth, side = path(t)
            capture.on_pose(t, (GOAL[0] + depth, GOAL[1] + side, GOAL[2]))

    assert captures['told'].tracker.pregrasp_t == pytest.approx(1.0, abs=0.02) and 'baseline' in captures['told'].frames
    assert captures['untold'].tracker.pregrasp_t is None
    assert captures['told'].residual()[0] == pytest.approx(0.02, abs=1e-3)       # the residual stays against the goal itself


def test_a_press_that_never_pushed_is_ready_after_the_settle_time():
    buffer  = FrameBuffer()
    capture = PressCapture(CaptureConfig(settle_s=0.5), buffer, left_arm=False)
    _play(capture, buffer, profile=[(0.0, -0.40), (0.6, -0.13), (1.0, -0.13)])

    assert capture.tracker.deepest_t is None and 'press' not in capture.frames
    assert not capture.ready(DURATION + 0.2)
    assert capture.ready(DURATION + 0.6)


# ========== Crops around the button box ==========

def test_crop_around_grows_the_box_about_its_centre_and_stays_in_the_image():
    frame = _jpeg((0, 0, 0), (640, 480))

    assert Image.open(io.BytesIO(crop_around(frame, (300, 200, 340, 220), 2.5))).size == (100, 50)
    assert Image.open(io.BytesIO(crop_around(frame, (0, 0, 10, 10), 2.5, min_side_px=48))).size == (29, 29)   # cut at the corner


def test_the_box_of_a_frame_is_its_own_or_the_latest_before_it():
    boxes = BoxBuffer()
    boxes.add(1.0, (1, 1, 2, 2))
    boxes.add(2.0, (3, 3, 4, 4))

    assert boxes.at(2.0, 1.5) == (3, 3, 4, 4)
    assert boxes.at(2.9, 1.5) == (3, 3, 4, 4)
    assert boxes.at(1.5, 1.5) == (1, 1, 2, 2)
    assert boxes.at(5.0, 1.5) is None


def test_the_crops_are_saved_and_a_hidden_button_borrows_the_baseline_box(tmp_path):
    buffer, boxes = FrameBuffer(keep_s=100), BoxBuffer(keep_s=100)
    capture       = PressCapture(CaptureConfig(box_max_age_s=0.5), buffer, left_arm=False, boxes=boxes)
    original      = buffer.add

    def add_frame(t, data):                             # 640x480 frames; the box is seen only before the push
        original(t, _jpeg((0, 0, 0), (640, 480)))

        if t < 0.5 * DURATION:
            boxes.add(t, (300, 200, 340, 220))

    buffer.add = add_frame
    _play(capture, buffer)

    assert set(capture.boxes) == {'baseline'}
    capture.save(str(tmp_path))

    assert Image.open(io.BytesIO(capture.crop('release')[0])).size == (100, 50)
    assert len(list(tmp_path.glob('press_*_crop.jpg'))) == 3


def test_the_swing_up_beside_the_goal_is_not_taken_for_the_push():
    """On the robot the hand rises from its rest pose about 20 cm below the button, at nearly the goal's depth, then
    backs off to the pre-grasp and pushes. Only the push after the pre-grasp counts."""
    tracker = PhaseTracker(CaptureConfig())
    tracker.set_goal(GOAL, (0, 0, 0, 1))
    path = [(0.0, -0.30, -0.25), (0.15, -0.02, -0.23), (0.3, -0.10, -0.02), (0.4, -0.10, 0.0),   # (phase, depth, z)
            (0.7, 0.0, 0.0), (0.75, 0.0, 0.0), (1.0, -0.10, 0.0)]

    for step in range(301):
        t = step / 100
        depth, z = (float(np.interp(t / DURATION, [p[0] for p in path], [p[i] for p in path])) for i in (1, 2))
        tracker.update(t, (GOAL[0] + depth, GOAL[1], GOAL[2] + z))

    assert tracker.pregrasp_t > 0.15 * DURATION
    assert tracker.max_depth == pytest.approx(0.0, abs=0.002)
    assert tracker.lateral_at_max == pytest.approx(0.0, abs=0.002)
    assert tracker.deepest_t == pytest.approx(0.7 * DURATION, abs=0.02)



def test_the_residual_is_in_the_goal_frame_x_in_y_left_z_up():
    """A goal facing +y (yaw 90 deg): its x is map +y, its left (y) is map -x"""
    capture = PressCapture(CaptureConfig(), FrameBuffer(), left_arm=False)
    yaw_90  = (0.0, 0.0, 0.7071068, 0.7071068)
    capture.on_goal(GOAL, yaw_90)

    for step in range(301):
        t     = step / 100
        depth = float(np.interp(t / DURATION, [p[0] for p in PRESS], [p[1] for p in PRESS]))
        # 5 mm short at the deepest point, 8 mm to the goal's right (map +x), 3 mm low
        capture.on_pose(t, (GOAL[0] + 0.008, GOAL[1] + depth - 0.005, GOAL[2] - 0.003))

    assert capture.residual() == pytest.approx([-0.005, -0.008, -0.003], abs=1e-4)



def test_a_service_field_path_can_index_a_sequence():
    from rcl_interfaces.srv import SetParameters
    from orchestrator.skills.messages import build_message

    contract = Registry.from_directory(CONTRACTS).get('set_press_offset')
    build_message(SetParameters.Request, contract, {'x': 0.0, 'y': 0.0, 'z': 0.0}, None)      # the contract stays reusable
    request  = build_message(SetParameters.Request, contract, {'x': 0.005, 'y': -0.0075, 'z': 0.0034}, None)

    assert [parameter.name for parameter in request.parameters] == ['arm_safety_distance_x', 'arm_safety_distance_y', 'arm_safety_distance_z']
    assert [parameter.value.type for parameter in request.parameters] == [3, 3, 3]
    assert [parameter.value.double_value for parameter in request.parameters] == [0.005, -0.0075, 0.0034]
