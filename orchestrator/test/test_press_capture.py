import io
import json
import base64
import threading
import urllib.request

from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from PIL import Image
from skill_registry import Registry

from orchestrator.press_capture import BoxBuffer, CaptureConfig, FrameBuffer, PhaseTracker, PressCapture, crop_around, judge_press, side_by_side
from orchestrator.cosmos_vlm_server import CosmosAnswerer, make_handler, parse_request

from data_paths import CONTRACTS


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
    times   = {name: t for name, (t, _) in capture.frames.items()}
    tracker = capture.tracker
    config  = capture.config

    assert set(times) == {'baseline', 'press', 'release'}
    assert tracker.pregrasp_t <= times['baseline'] < tracker.pregrasp_t + 1 / 6
    assert tracker.deepest_t + config.press_delay_s <= times['press'] < tracker.deepest_t + config.press_delay_s + 1 / 6
    assert tracker.release_t + config.release_delay_s <= times['release'] < tracker.release_t + config.release_delay_s + 1 / 6


def test_a_press_that_never_pushed_is_ready_after_the_settle_time():
    buffer  = FrameBuffer()
    capture = PressCapture(CaptureConfig(settle_s=0.5), buffer, left_arm=False)
    _play(capture, buffer, profile=[(0.0, -0.40), (0.6, -0.13), (1.0, -0.13)])

    assert capture.tracker.deepest_t is None and 'press' not in capture.frames
    assert not capture.ready(DURATION + 0.2)
    assert capture.ready(DURATION + 0.6)


def test_side_by_side_puts_the_two_photos_in_one_jpeg():
    joined = Image.open(io.BytesIO(side_by_side(_jpeg((255, 0, 0), (64, 48)), _jpeg((0, 0, 255), (32, 48)), height=96)))

    assert joined.size == (128 + 64, 96)
    assert joined.getpixel((10, 50))[0] > 200 and joined.getpixel((180, 50))[2] > 200


# ========== The check on the kept frames ==========

class FakeVlm:
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.asked   = []

    def ask(self, image: bytes, question: str, choices: list[str]) -> str:
        self.asked.append((Image.open(io.BytesIO(image)).size, question))
        return self.answers.pop(0)


def _check(capture: PressCapture, answers: list[str], tmp_path) -> tuple[bool, str, FakeVlm]:
    vlm       = FakeVlm(answers)
    interface = Registry.from_directory(CONTRACTS).get('button_lit').interface
    success, message, _ = judge_press(capture, interface, {'target_description': 'up button'}, vlm.ask, None, str(tmp_path))

    return success, message, vlm


def test_lit_in_either_later_frame_is_success_and_the_frames_are_saved(tmp_path):
    buffer  = FrameBuffer(keep_s=100)
    capture = PressCapture(CaptureConfig(), buffer, left_arm=False)
    _play(capture, buffer)

    status, message, vlm = _check(capture, ['not_lit', 'lit'], tmp_path)

    question = Registry.from_directory(CONTRACTS).get('button_lit').interface.compare_question.format(target_description='up button')

    assert status and 'release: lit' in message
    assert all(asked == question for _, asked in vlm.asked)
    assert vlm.asked[0][0][0] == 2 * vlm.asked[0][0][1] * 64 // 48          # two photos side by side
    assert len(list(tmp_path.glob('press_*.jpg'))) == 3


def test_not_lit_in_both_is_failure(tmp_path):
    buffer  = FrameBuffer(keep_s=100)
    capture = PressCapture(CaptureConfig(), buffer, left_arm=False)
    _play(capture, buffer)

    status, message, _ = _check(capture, ['not_lit', 'unsure'], tmp_path)

    assert not status and message.startswith('press: not_lit, release: unsure')


def test_without_a_baseline_the_photos_are_asked_alone(tmp_path):
    buffer  = FrameBuffer(keep_s=100)
    capture = PressCapture(CaptureConfig(pregrasp_tolerance_m=0.0), buffer, left_arm=False)      # never at the pre-grasp
    _play(capture, buffer)

    status, message, vlm = _check(capture, ['lit'], tmp_path)

    assert status and 'no baseline frame' in message
    assert vlm.asked[0][1] == Registry.from_directory(CONTRACTS).get('button_lit').interface.question.format(target_description='up button')


# ========== cosmos_vlm_server ==========

class FakeCosmos:
    def __init__(self, busy_times: int = 0):
        self.busy_times = busy_times
        self.prompts    = []

    def query(self, image, prompt, max_tokens):
        if self.busy_times:
            self.busy_times -= 1
            raise RuntimeError('resident daemon busy')

        self.prompts.append((image.shape, prompt, max_tokens))
        return 'lit'


def _request(question: str) -> dict:
    url = 'data:image/jpeg;base64,' + base64.b64encode(_jpeg((10, 20, 30))).decode()
    return {'model': 'local', 'max_tokens': 64, 'messages': [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': url}}, {'type': 'text', 'text': question}]}]}


def test_the_cosmos_server_answers_one_image_and_waits_out_a_busy_daemon():
    image, text, max_tokens = parse_request(_request('Is it lit?'))
    model                   = FakeCosmos(busy_times=2)
    answerer                = CosmosAnswerer(model, busy_wait_s=5.0)

    assert text == 'Is it lit?' and max_tokens == 64
    assert answerer.answer(image, text, max_tokens) == 'lit'
    assert model.prompts[0][0] == (48, 64, 3)


def test_the_cosmos_server_speaks_chat_completions():
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(CosmosAnswerer(FakeCosmos())))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                                         data=json.dumps(_request('Is it lit?')).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            reply = json.loads(response.read())
    finally:
        server.shutdown()

    assert reply['choices'][0]['message']['content'] == 'lit'


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


def test_the_check_asks_about_crops_and_a_hidden_button_borrows_the_baseline_box(tmp_path):
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
    success, message, vlm = _check(capture, ['not_lit', 'not_lit'], tmp_path)

    assert not success and 'no button box' not in message
    height = int(capture.config.compare_height_px)

    assert vlm.asked[0][0] == (2 * round(100 * height / 50), height)               # two 100x50 crops, side by side
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
    from orchestrator.ros_skill_runner import build_message

    contract = Registry.from_directory(CONTRACTS).get('set_press_offset')
    build_message(SetParameters.Request, contract, {'x': 0.0, 'y': 0.0, 'z': 0.0}, None)      # the contract stays reusable
    request  = build_message(SetParameters.Request, contract, {'x': 0.005, 'y': -0.0075, 'z': 0.0034}, None)

    assert [parameter.name for parameter in request.parameters] == ['arm_safety_distance_x', 'arm_safety_distance_y', 'arm_safety_distance_z']
    assert [parameter.value.type for parameter in request.parameters] == [3, 3, 3]
    assert [parameter.value.double_value for parameter in request.parameters] == [0.005, -0.0075, 0.0034]
