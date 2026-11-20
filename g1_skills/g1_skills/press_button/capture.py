"""Three camera frames around one press, for the check after it.

  baseline  the first frame once the end effector has reached the pre-grasp (the button is not pressed yet)
  press     the first frame at least press_delay_s after the deepest point of the push
  release   the first frame at least release_delay_s after the retreat began

Each frame is also saved as a crop around the button: track_anything's box for that frame (the 2D detection with the
same stamp, or the latest before it), grown crop_scale times about its centre. A frame with no box of its own uses
the baseline's box, since the hand covers the button during the push.

The phases come from the end-effector pose (TF from the goal's frame to right_ee or left_ee, sampled at 100 Hz) against the goal
the arm was sent (the action feedback's current_goal): depth = (ee - goal) . x_goal is 0 at the press pose and
-approach_offset at the pre-grasp. The deepest point is known once the depth has dropped retreat_drop below its
maximum, or when the action ends after a push without a retreat, so frames are kept in a short buffer and chosen afterwards. Given a directory, each frame is written there
as soon as it is chosen, so whoever judges the press can copy it before the press ends. Times are ROS times (header stamps).
"""

import io
import math
import threading

from pathlib import Path
from dataclasses import dataclass, field

import numpy as np

from PIL import Image, ImageDraw

from orchestrator.sensing import FrameBuffer

from ..lib.tracking import BoxBuffer


FRAMES = ('baseline', 'press', 'release')


@dataclass
class CaptureConfig:
    approach_offset_m:    float = 0.10      # visual_servo_planner approach_offset
    pregrasp_tolerance_m: float = 0.02
    lateral_tolerance_m:  float = 0.03
    retreat_drop_m:       float = 0.005     # the depth below its maximum that marks the retreat
    press_delay_s:        float = 0.005
    release_delay_s:      float = 0.5
    buffer_s:             float = 8.0       # frames kept; covers the push and the release delay
    settle_s:             float = 3.0       # after the action ends, how long to wait for frames still to come
    crop_scale:           float = 2.5       # the crop's sides, as a multiple of the button box's
    min_crop_px:          float = 48.0
    box_max_age_s:        float = 1.5       # a box older than this, before its frame, is not used for it
    tip_shift_max_age_s:  float = 1.0       # a gripper estimate this close to the deepest point is used alone
    # Else the held median, as move_to_pose corrects its target (g1_pilot gripper_error_history.h): the per-axis median of
    # the last tip_shift_hold_count fits within tip_shift_hold_s before the deepest point, each under tip_shift_max_error_m.
    # Without it the residual kept move_to_pose's correction as an error (29 of 33 presses on 2026-11-19), and recovery
    # set it back: an encoder tip 24 mm low brought a 24 mm higher offset where move_to_pose had lowered 17 mm
    tip_shift_hold_s:     float = 60.0      # move_to_pose gripper_estimate_hold_time
    tip_shift_hold_count: float = 5         # GripperErrorHistory's capacity
    tip_shift_max_error_m: float = 0.03     # move_to_pose gripper_estimate_max_error
    camera_pose_max_age_s: float = 0.1      # a camera pose further than this from its frame is not used for it

    @classmethod
    def from_contract(cls, raw: dict) -> 'CaptureConfig':
        return cls(**{key: float(value) for key, value in raw.items() if key in cls.__dataclass_fields__})


def x_axis(quaternion_xyzw) -> np.ndarray:
    x, y, z, w = quaternion_xyzw

    return np.array([1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)])


def press_axes(axis) -> np.ndarray:
    """Rows x, y, z of the frame the press offset is set in: x the goal's +X made level (into the panel), z up, y = z cross x"""
    x = np.array([axis[0], axis[1], 0.0])
    x = x / (np.linalg.norm(x) or 1.0)
    z = np.array([0.0, 0.0, 1.0])

    return np.stack([x, np.cross(z, x), z])


def rotation(quaternion_xyzw) -> np.ndarray:
    x, y, z, w = quaternion_xyzw

    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


class PhaseTracker:
    """When the end effector reached the pre-grasp, pushed deepest, and began to retreat"""

    def __init__(self, config: CaptureConfig):
        self.config     = config
        self.goal       = None
        self.axis       = None
        self.shift      = np.zeros(3)
        self.pregrasp_t = None
        self.deepest_t  = None
        self.release_t  = None
        self.max_depth  = -math.inf
        self.lateral_at_max = None    # sideways distance from the goal at the deepest point
        self.offset_at_max  = None    # end effector minus goal at the deepest point, in map-like axes (foot_world)
        self._max_t     = None

    def set_goal(self, position, quaternion_xyzw, press_offset=(0.0, 0.0, 0.0)):
        """press_offset: the press offset move_to_pose adds in the goal's own frame. The pre-grasp sits approach_offset in
        front of the offset goal; depth, lateral and the residual stay measured against the goal itself"""
        self.goal, self.axis = np.asarray(position, dtype=float), x_axis(quaternion_xyzw)
        self.shift = rotation(quaternion_xyzw) @ np.asarray(press_offset, dtype=float)

    def update(self, t: float, position):
        if self.goal is None or self.release_t is not None:
            return

        offset  = np.asarray(position, dtype=float) - self.goal
        depth   = float(offset @ self.axis)
        lateral = float(np.linalg.norm(offset - depth * self.axis))
        planned = offset - self.shift
        plan_depth = float(planned @ self.axis)

        if (self.pregrasp_t is None and abs(plan_depth + self.config.approach_offset_m) < self.config.pregrasp_tolerance_m
                and float(np.linalg.norm(planned - plan_depth * self.axis)) < self.config.lateral_tolerance_m):
            self.pregrasp_t = t

        # Before the pre-grasp the hand is still swinging up from its rest pose and can pass the goal depth far beside
        # the button (once -20.6 mm deep, 229 mm sideways), which ended the press there and froze the tracker
        # before the real push. Until the pre-grasp is seen, only a sample near the button axis can be the push.
        if self.pregrasp_t is None and lateral >= self.config.lateral_tolerance_m:
            return

        if depth > self.max_depth:
            self.max_depth, self._max_t, self.lateral_at_max, self.offset_at_max = depth, t, lateral, offset

        pushed = self.max_depth > -self.config.approach_offset_m / 2

        if pushed and depth < self.max_depth - self.config.retreat_drop_m:
            self.deepest_t, self.release_t = self._max_t, t

    def end(self, t: float):
        """The action ended. move_to_pose ends a press without a retreat when no target update came during the approach,
        so a push not seen to retreat takes its deepest point so far, and the end of the action stands for the retreat"""
        if self.release_t is None and self._max_t is not None and self.max_depth > -self.config.approach_offset_m / 2:
            self.deepest_t, self.release_t = self._max_t, t


class PressCapture:
    """One press: fed poses and goals while the arm moves; ready once its frames are chosen or time ran out"""

    def __init__(self, config: CaptureConfig, buffer: FrameBuffer, left_arm: bool, boxes: BoxBuffer | None = None,
                 directory: str | None = None, press_offset=(0.0, 0.0, 0.0)):
        self.config   = config
        self.press_offset = tuple(press_offset)
        self.directory = directory      # where each frame is written once chosen; None: only on save()
        self.buffer   = buffer
        self.left_arm = left_arm
        self.tracker  = PhaseTracker(config)
        self.box_feed = boxes
        self.frames   = {}              # name -> (ROS time, bytes)
        self.boxes    = {}              # name -> the button box of that frame, when there was one
        self.ended_t  = None            # when the action ended, success or not
        self.goal_frame = None          # the goal's frame, where end-effector poses are looked up
        self.tip_shifts = []            # (ROS time, depth-fitted minus encoder gripper position in the goal's frame)
        self.camera_poses = []          # (ROS time, rotation, translation): the goal's frame into the camera's optical frame
        self.saved    = {}              # name -> path, once saved
        self.aimed    = None            # (k, the aim crops): made once per press, for the recovery
        self._lock    = threading.Lock()

    def on_goal(self, position, quaternion_xyzw, frame: str | None = None):
        with self._lock:
            self.tracker.set_goal(position, quaternion_xyzw, self.press_offset)
            self.goal_frame = frame or self.goal_frame

    def on_pose(self, t: float, position):
        with self._lock:
            self.tracker.update(t, position)

        self.choose()

    def on_tip_shift(self, t: float, shift):
        with self._lock:
            self.tip_shifts.append((t, np.asarray(shift, dtype=float)))

    def tip_shift_at_max(self):
        """The gripper estimate's shift at the deepest point, or None without one (tip_shift_source_at_max)"""
        return self.tip_shift_source_at_max()[0]

    def tip_shift_source_at_max(self):
        """(shift, source) at the deepest point: the fit nearest it when one is within tip_shift_max_age_s ('gripper
        estimate'); else the per-axis median of the last tip_shift_hold_count fits within tip_shift_hold_s before it
        ('gripper estimate, held median of N'); else (None, 'encoders'). Fits over tip_shift_max_error_m are not used"""
        config = self.config
        t      = self.tracker._max_t

        if t is None:
            return None, 'encoders'

        fits = [(stamp, shift) for stamp, shift in self.tip_shifts
                if float(np.linalg.norm(shift)) <= config.tip_shift_max_error_m]

        if not fits:
            return None, 'encoders'

        stamp, shift = min(fits, key=lambda item: abs(item[0] - t))

        if abs(stamp - t) <= config.tip_shift_max_age_s:
            return shift, 'gripper estimate'

        held = [shift for stamp, shift in fits if 0.0 <= t - stamp <= config.tip_shift_hold_s]
        held = held[-int(config.tip_shift_hold_count):]

        if not held:
            return None, 'encoders'

        return np.median(np.array(held), axis=0), f'gripper estimate, held median of {len(held)}'

    def on_camera_pose(self, t: float, rotation_matrix, translation):
        with self._lock:
            self.camera_poses.append((t, np.asarray(rotation_matrix, dtype=float), np.asarray(translation, dtype=float)))

    def camera_pose_at(self, name: str):
        """(rotation, translation) of the camera when frame name was taken, or None when no pose is close enough"""
        with self._lock:
            if name not in self.frames or not self.camera_poses:
                return None

            t = self.frames[name][0]
            stamp, turn, shift = min(self.camera_poses, key=lambda item: abs(item[0] - t))

        return (turn, shift) if abs(stamp - t) <= self.config.camera_pose_max_age_s else None

    def aim(self, press: dict, k, directory: str) -> dict | None:
        """The aim crops of this press (aim_view), made once; None without the intrinsics or a camera pose"""
        if k is None:
            return None

        if self.aimed is None:
            poses      = {name: self.camera_pose_at(name) for name in AIM_FRAMES.values()}
            self.aimed = (k, aim_view(press, poses, k, directory))

        return self.aimed[1]

    def on_action_end(self, t: float):
        with self._lock:
            self.tracker.end(t)

        self.ended_t = t
        self.choose()

    def wanted_times(self) -> dict:
        tracker = self.tracker
        wanted  = {}

        if tracker.pregrasp_t is not None:
            wanted['baseline'] = tracker.pregrasp_t

        if tracker.deepest_t is not None:
            wanted['press']   = tracker.deepest_t + self.config.press_delay_s
            wanted['release'] = tracker.release_t + self.config.release_delay_s

        return wanted

    def choose(self):
        with self._lock:
            for name, t in self.wanted_times().items():
                if name not in self.frames:
                    frame = self.buffer.first_after(t)

                    if frame is not None:
                        self.frames[name] = frame
                        box = self.box_feed.at(frame[0], self.config.box_max_age_s) if self.box_feed else None

                        if box is not None:
                            self.boxes[name] = box

                        if self.directory:
                            self._write(name, self.directory)

    def crop(self, name: str) -> tuple[bytes, tuple | None]:
        """The frame cropped around its button box, or the baseline's box; the whole frame if neither exists"""
        box = self.boxes.get(name) or self.boxes.get('baseline')
        data = self.frames[name][1]

        return (crop_around(data, box, self.config.crop_scale, self.config.min_crop_px), box) if box else (data, None)

    def ready(self, now: float) -> bool:
        """All three frames chosen, or the action ended settle_s ago"""
        self.choose()

        return len(self.frames) == len(FRAMES) or (self.ended_t is not None and now - self.ended_t > self.config.settle_s)

    def save(self, directory: str) -> dict:
        """Write each chosen frame not written yet, as <directory>/press_<time>_<name>.jpg; return name -> path"""
        with self._lock:
            for name in list(self.frames):
                if name not in self.saved:
                    self._write(name, directory)

            return dict(self.saved)

    def _write(self, name: str, directory: str):
        folder = Path(directory).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        t, data = self.frames[name]
        cropped, box = self.crop(name)

        self.saved[name] = _write_whole(folder / f'press_{t:.3f}_{name}.jpg', data)

        if box is not None:
            self.saved[f'{name}_crop'] = _write_whole(folder / f'press_{t:.3f}_{name}_crop.jpg', cropped)

    def residual(self) -> list | None:
        """End effector minus goal at the deepest point, in the goal's frame as move_to_pose applies the press offset:
        x along the goal's +X (into the panel), z up, y = z cross x (to the left). The negative is the correction.
        The encoder pose is moved by the gripper estimate's shift when one is near, so the residual is the real tip's"""
        tracker = self.tracker

        if tracker.offset_at_max is None:
            return None

        shift  = self.tip_shift_at_max()
        offset = tracker.offset_at_max + (shift if shift is not None else 0.0)

        return [round(float(value), 4) for value in press_axes(tracker.axis) @ offset]

    def events(self) -> dict:
        tracker = self.tracker

        return {
            'pregrasp_t': tracker.pregrasp_t,
            'deepest_t':  tracker.deepest_t,
            'release_t':  tracker.release_t,
            'max_depth_m': None if math.isinf(tracker.max_depth) else round(tracker.max_depth, 4),
            'lateral_at_max_m': None if tracker.lateral_at_max is None else round(tracker.lateral_at_max, 4),
            'residual_m': self.residual(),
            'residual_from': self.tip_shift_source_at_max()[1],
            'frames':     {name: round(t, 3) for name, (t, _) in self.frames.items()},
            'boxes':      {name: [round(value, 1) for value in box] for name, box in self.boxes.items()},
        }


# ========== Aim crops ==========

AIM_HALF_WIDTH_PX  = 110            # the crop around the goal, in camera pixels
AIM_HALF_HEIGHT_PX = 80
AIM_SCALE          = 2              # the crop is upscaled this much for the model
AIM_FRAMES         = {'before_aim': 'baseline', 'contact_aim': 'press'}     # crop name -> the press frame it is cut from


def aim_view(press: dict, camera_poses: dict, k, directory: str) -> dict | None:
    """The press's baseline and press frames cropped around its goal, as files in directory, each with its scale at the
    button and the image's right and down in the press offset's frame (x in, y left, z up). camera_poses: press frame
    name -> (rotation, translation) from the goal's frame into the camera's optical frame when that frame was taken;
    k: the camera's intrinsics, 3x3. None without a goal, a frame or its camera pose"""
    frames = press.get('frames') or {}
    k      = np.asarray(k, dtype=float)

    if press.get('goal') is None or press.get('goal_axis') is None:
        return None

    if any(source not in frames or camera_poses.get(source) is None for source in AIM_FRAMES.values()):
        return None

    axes = press_axes(press['goal_axis'])
    view = {'frames': {}, 'geometry': {}, 'size_px': [2 * AIM_SCALE * AIM_HALF_WIDTH_PX, 2 * AIM_SCALE * AIM_HALF_HEIGHT_PX]}

    for name, source in AIM_FRAMES.items():
        turn, shift = camera_poses[source]
        goal        = turn @ np.asarray(press['goal'], dtype=float) + shift

        if goal[2] <= 0.0:
            return None

        path = Path(directory).expanduser() / f'{Path(frames[source]).stem}_aim.jpg'
        _crop_at(frames[source], (k @ goal)[:2] / goal[2], path)
        view['frames'][name]   = str(path)
        view['geometry'][name] = {
            'mm_per_px':      round(float(goal[2] * 1000.0 / k[0, 0] / AIM_SCALE), 4),
            'right_in_press': [round(float(value), 3) for value in axes @ turn.T @ np.array([1.0, 0.0, 0.0])],
            'down_in_press':  [round(float(value), 3) for value in axes @ turn.T @ np.array([0.0, 1.0, 0.0])],
        }

    residual = (press.get('capture') or {}).get('residual_m')

    if residual is not None:            # the estimated fingertip: goal plus the residual, back from the press frame
        turn, shift = camera_poses[AIM_FRAMES['contact_aim']]
        goal        = turn @ np.asarray(press['goal'], dtype=float) + shift
        tip         = turn @ (np.asarray(press['goal'], dtype=float) + axes.T @ np.asarray(residual, dtype=float)) + shift

        if tip[2] > 0.0:
            view['estimate_marked'] = _mark_in_crop(view['frames']['contact_aim'], (k @ goal)[:2] / goal[2],
                                                    (k @ tip)[:2] / tip[2])

    return view


def _mark_in_crop(path: str, centre, point) -> bool:
    """Draw a cyan crosshair at camera pixel point on the aim crop cut around centre; False when it falls outside"""
    left, top = int(centre[0]) - AIM_HALF_WIDTH_PX, int(centre[1]) - AIM_HALF_HEIGHT_PX
    x, y      = (point[0] - left) * AIM_SCALE, (point[1] - top) * AIM_SCALE
    image     = Image.open(path).convert('RGB')

    if not (0 <= x < image.width and 0 <= y < image.height):
        return False

    draw = ImageDraw.Draw(image)
    draw.line((x - 14, y, x + 14, y), fill=(0, 255, 255), width=2)
    draw.line((x, y - 14, x, y + 14), fill=(0, 255, 255), width=2)
    draw.ellipse((x - 9, y - 9, x + 9, y + 9), outline=(0, 255, 255), width=2)
    image.save(path, quality=95)

    return True


def _crop_at(source: str, centre, path: Path):
    left, top = int(centre[0]) - AIM_HALF_WIDTH_PX, int(centre[1]) - AIM_HALF_HEIGHT_PX
    image     = Image.open(source).convert('RGB').crop((left, top, left + 2 * AIM_HALF_WIDTH_PX, top + 2 * AIM_HALF_HEIGHT_PX))
    image.resize((2 * AIM_SCALE * AIM_HALF_WIDTH_PX, 2 * AIM_SCALE * AIM_HALF_HEIGHT_PX), Image.BICUBIC).save(path, quality=95)


# ========== Images ==========

def _write_whole(path: Path, data: bytes) -> str:
    """Write under a temporary name, then rename, so a reader never sees half a file"""
    part = path.with_name(f'.{path.name}.part')
    part.write_bytes(data)
    part.replace(path)

    return str(path)


def crop_around(data: bytes, box: tuple, scale: float, min_side_px: float = 48.0) -> bytes:
    """A JPEG of the region around box (x_min, y_min, x_max, y_max): the box grown scale times about its centre,
    at least min_side_px on a side, kept inside the image"""
    image                        = Image.open(io.BytesIO(data)).convert('RGB')
    x_min, y_min, x_max, y_max   = box
    centre_x, centre_y           = (x_min + x_max) / 2, (y_min + y_max) / 2
    half_width                   = max((x_max - x_min) * scale, min_side_px) / 2
    half_height                  = max((y_max - y_min) * scale, min_side_px) / 2
    left, top                    = max(0, round(centre_x - half_width)), max(0, round(centre_y - half_height))
    right, bottom                = min(image.width, round(centre_x + half_width)), min(image.height, round(centre_y + half_height))

    if right <= left or bottom <= top:
        return data                                     # the box lies outside this image: keep it whole

    buffer = io.BytesIO()
    image.crop((left, top, right, bottom)).save(buffer, format='JPEG', quality=95)

    return buffer.getvalue()
