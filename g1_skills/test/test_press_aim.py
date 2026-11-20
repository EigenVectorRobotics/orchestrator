import numpy as np
import pytest

from PIL import Image

from orchestrator.sensing import FrameBuffer

from g1_skills.lib.press import PressState
from g1_skills.press_button.capture import CaptureConfig, PressCapture, aim_view, press_axes

from fakes import FakeCtx

K = np.array([[905.6, 0.0, 655.8], [0.0, 904.6, 381.5], [0.0, 0.0, 1.0]])
# The camera looks along the press +X: optical z = press x, optical x (right) = press -y, optical y (down) = press -z
CAMERA_FROM_PRESS = np.array([[0.0, -1.0, 0.0], [0.0, 0.0, -1.0], [1.0, 0.0, 0.0]])
LEVEL             = (CAMERA_FROM_PRESS, np.zeros(3))


def _press(tmp_path):
    frames = {}

    for name in ('baseline', 'press'):
        path = tmp_path / f'press_1.000_{name}.jpg'
        Image.new('RGB', (1280, 720), (90, 90, 90)).save(path)
        frames[name] = str(path)

    return {'goal': [0.5, 0.0, 0.0], 'goal_axis': [1.0, 0.0, 0.0], 'press_offset_m': [0.003, 0.0, 0.001], 'frames': frames}


def test_each_crop_carries_its_scale_and_the_image_axes_in_the_press_frame(tmp_path):
    view = aim_view(_press(tmp_path), {'baseline': LEVEL, 'press': LEVEL}, K, str(tmp_path))

    assert set(view['frames']) == {'before_aim', 'contact_aim'}
    assert view['geometry']['contact_aim']['mm_per_px'] == pytest.approx(500.0 / 905.6 / 2, abs=1e-4)
    assert view['geometry']['contact_aim']['right_in_press'] == pytest.approx([0.0, -1.0, 0.0])
    assert view['geometry']['contact_aim']['down_in_press'] == pytest.approx([0.0, 0.0, -1.0])
    assert Image.open(view['frames']['contact_aim']).size == tuple(view['size_px']) == (440, 320)


def test_a_camera_that_moved_between_the_frames_gives_each_crop_its_own_geometry(tmp_path):
    closer = (CAMERA_FROM_PRESS, np.array([0.0, 0.0, -0.1]))
    view   = aim_view(_press(tmp_path), {'baseline': LEVEL, 'press': closer}, K, str(tmp_path))

    assert view['geometry']['contact_aim']['mm_per_px'] < view['geometry']['before_aim']['mm_per_px']


def test_without_a_goal_a_frame_or_a_camera_pose_there_is_no_view(tmp_path):
    press = _press(tmp_path)
    poses = {'baseline': LEVEL, 'press': LEVEL}

    assert aim_view({**press, 'goal': None}, poses, K, str(tmp_path)) is None
    assert aim_view({**press, 'frames': {'press': press['frames']['press']}}, poses, K, str(tmp_path)) is None
    assert aim_view(press, {'baseline': LEVEL, 'press': None}, K, str(tmp_path)) is None


def test_the_press_axes_are_level_whatever_the_goal_pitch():
    axes = press_axes([0.8, 0.0, -0.6])

    assert axes[0] == pytest.approx([1.0, 0.0, 0.0]) and axes[2] == pytest.approx([0.0, 0.0, 1.0])


def test_the_press_evidence_carries_the_goal_axis_and_its_aim_crops(tmp_path):
    ctx     = FakeCtx(artifacts_dir=str(tmp_path))
    state   = PressState(ctx)
    capture = PressCapture(CaptureConfig(settle_s=0.0), FrameBuffer(), left_arm=False, directory=str(tmp_path))
    capture.on_goal((0.5, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), 'foot_world')
    capture.on_action_end(0.0)
    state.last_capture = capture

    assert state.evidence(wait_s=0.0)['goal_axis'] == [1.0, 0.0, 0.0]
    assert state.evidence(wait_s=0.0)['aim'] is None                      # no intrinsics yet: no crops

    ctx.camera.k = K.tolist()
    assert state.evidence(wait_s=0.0)['aim'] is None                      # no frames or camera poses for this press


def test_contact_aim_marks_the_estimated_fingertip_at_the_goal_plus_the_residual(tmp_path):
    """The residual 10 mm left and 20 mm up of the goal lands left of and above the crop centre, scaled by mm per px"""
    press = {**_press(tmp_path), 'capture': {'residual_m': [0.0, 0.010, 0.020]}}
    view  = aim_view(press, {'baseline': LEVEL, 'press': LEVEL}, K, str(tmp_path))
    mm    = view['geometry']['contact_aim']['mm_per_px']
    x, y  = round(220 - 10.0 / mm), round(160 - 20.0 / mm)
    image = Image.open(view['frames']['contact_aim'])

    assert view['estimate_marked'] is True
    assert image.getpixel((x, y))[1:] == pytest.approx((255, 255), abs=60) and image.getpixel((x, y))[0] < 100
    assert image.getpixel((220, 160)) == pytest.approx((90, 90, 90), abs=10)
    assert Image.open(view['frames']['before_aim']).getpixel((x, y)) == pytest.approx((90, 90, 90), abs=10)


def test_an_estimate_outside_the_crop_is_not_marked(tmp_path):
    press = {**_press(tmp_path), 'capture': {'residual_m': [0.0, 0.0, 0.5]}}

    assert aim_view(press, {'baseline': LEVEL, 'press': LEVEL}, K, str(tmp_path))['estimate_marked'] is False
    assert 'estimate_marked' not in aim_view(_press(tmp_path), {'baseline': LEVEL, 'press': LEVEL}, K, str(tmp_path))
