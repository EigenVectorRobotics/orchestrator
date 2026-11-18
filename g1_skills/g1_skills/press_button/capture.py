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

    @classmethod
    def from_contract(cls, raw: dict) -> 'CaptureConfig':
        return cls(**{key: float(value) for key, value in raw.items() if key in cls.__dataclass_fields__})


def x_axis(quaternion_xyzw) -> np.ndarray:
    x, y, z, w = quaternion_xyzw

    return np.array([1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)])


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
        self.saved    = {}              # name -> path, once saved
        self._lock    = threading.Lock()

    def on_goal(self, position, quaternion_xyzw, frame: str | None = None):
        with self._lock:
            self.tracker.set_goal(position, quaternion_xyzw, self.press_offset)
            self.goal_frame = frame or self.goal_frame

    def on_pose(self, t: float, position):
        with self._lock:
            self.tracker.update(t, position)

        self.choose()

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
        x along the goal's +X (into the panel), z up, y = z cross x (to the left). The negative is the correction"""
        tracker = self.tracker

        if tracker.offset_at_max is None:
            return None

        x = np.array([tracker.axis[0], tracker.axis[1], 0.0])
        x = x / (np.linalg.norm(x) or 1.0)
        z = np.array([0.0, 0.0, 1.0])
        y = np.cross(z, x)

        return [round(float(tracker.offset_at_max @ axis), 4) for axis in (x, y, z)]

    def events(self) -> dict:
        tracker = self.tracker

        return {
            'pregrasp_t': tracker.pregrasp_t,
            'deepest_t':  tracker.deepest_t,
            'release_t':  tracker.release_t,
            'max_depth_m': None if math.isinf(tracker.max_depth) else round(tracker.max_depth, 4),
            'lateral_at_max_m': None if tracker.lateral_at_max is None else round(tracker.lateral_at_max, 4),
            'residual_m': self.residual(),
            'frames':     {name: round(t, 3) for name, (t, _) in self.frames.items()},
            'boxes':      {name: [round(value, 1) for value in box] for name, box in self.boxes.items()},
        }


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
    from PIL import Image

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
