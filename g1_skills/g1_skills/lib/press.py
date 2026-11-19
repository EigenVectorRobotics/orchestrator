"""What the press skills share: the press offset in force and the last press, and its frames for an agent call."""

import time

from pathlib import Path

from orchestrator.agent.api import CallImage


class PressState:
    """What the press skills share in one executor: the press offset in force and the last press's capture"""

    def __init__(self, ctx):
        self.ctx           = ctx
        self.offset        = None       # the last set_press_offset, which moves move_to_pose's pre-grasp; None: not set yet
        self.last_capture  = None       # the frames of the last press, for the agent

    def evidence(self, wait_s: float = 8.0) -> dict | None:
        """The last press for an agent call: once its frames are chosen (or wait_s has passed), save them and return
        their paths with the phase times, depth and sideways distance"""
        capture = self.last_capture

        if capture is None:
            return None

        deadline = time.monotonic() + wait_s

        while not capture.ready(self.ctx.now()) and time.monotonic() < deadline:
            time.sleep(0.1)

        tracker = capture.tracker
        press   = {
            'left_arm':       capture.left_arm,
            'goal':           None if tracker.goal is None else [round(float(value), 4) for value in tracker.goal],
            'goal_axis':      None if tracker.goal is None else [round(float(value), 4) for value in tracker.axis],
            'press_offset_m': [round(float(value), 4) for value in capture.press_offset],   # the offset this press used
            'capture':        capture.events(),
            'frames':         capture.save(self.ctx.artifacts_dir),
        }

        try:                                            # the crops around the goal, for the recovery's offset
            press['aim'] = capture.aim(press, self.ctx.camera.k, self.ctx.artifacts_dir)
        except (OSError, ValueError) as error:
            self.ctx.logger.warn(f'no aim crops: {error}')
            press['aim'] = None

        return press


def press_state(ctx) -> PressState:
    """The executor's one PressState"""
    return ctx.shared('press', lambda: PressState(ctx))


def since_last_pressed(history: list[dict]) -> list[dict]:
    """The records after the last press judged pressed: the attempts at the press now under way"""
    pressed = [index for index, record in enumerate(history) if record.get('skill') == 'press_judge' and record.get('decision') == 'pressed']

    return history[pressed[-1] + 1:] if pressed else history


def offset_in_force(history: list[dict], press_offset: list | None = None) -> list[float]:
    """The press offset (x in, y left, z up) the last press used: press_offset from its capture, else the last
    set_press_offset since the last press judged pressed (0 when there is none)"""
    if press_offset is not None and len(press_offset) == 3:
        return [float(value) for value in press_offset]

    offsets = [record.get('inputs') or {} for record in since_last_pressed(history) if record.get('skill') == 'set_press_offset']
    last    = offsets[-1] if offsets else {}

    return [float(last.get(axis, 0.0)) for axis in 'xyz']


def press_images(press: dict | None, aim: bool = False) -> list[CallImage]:
    """The saved frames (and crops) of a press, as a call's images; with aim, its aim crops after them"""
    images = []
    named  = dict((press or {}).get('frames') or {})

    if aim:
        named.update(((press or {}).get('aim') or {}).get('frames') or {})

    for name, path in named.items():
        try:
            images.append(CallImage(name, 'image/jpeg', Path(path).read_bytes(), path))
        except OSError:
            continue

    return images
