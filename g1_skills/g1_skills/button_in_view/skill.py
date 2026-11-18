"""Before the first press: is the button in the camera's view from where the robot stands, and which arm presses it?

A poor stance comes from the walk before the press: the robot stopped too close, so
the button fell below the frame, or too far, so the arm stops short. These checks catch it before press_button
fails. They run in the executor on what track_anything already publishes (its 2D box, its 3D target pose) and TF,
with no VLM call of their own; button_in_view seeds the track that feeds them.

  in_view   the target's latest box is fresh and lies inside the image, a margin away from each edge
"""

import time

from geometry_msgs.msg import PoseStamped
from rosidl_runtime_py.utilities import get_message

from orchestrator.skills.api import Handle, Skill
from orchestrator.tree.engine import Status

from ..lib.tracking import Tracking, tracking


# ========== The checks ==========

VIEW_DEFAULTS  = {'max_age_s': 1.0, 'margin_frac': 0.05}
ARM_DEFAULTS   = {'left_arm_min_y_m': 0.10}


def _limits(defaults: dict, limits: dict | None, inputs: dict | None) -> dict:
    """The contract's limits over the defaults, and the tree's inputs over both (a panel's own range)"""
    merged = {**defaults, **(limits or {})}
    merged.update({key: value for key, value in (inputs or {}).items() if key in defaults and value is not None})

    return {key: float(value) for key, value in merged.items()}


def judge_view(box: tuple | None, box_age_s: float | None, image_size: tuple | None, limits: dict | None = None,
               inputs: dict | None = None) -> tuple[bool, str, dict]:
    """box (x_min, y_min, x_max, y_max) in pixels, its age in seconds, image_size (width, height)"""
    limit = _limits(VIEW_DEFAULTS, limits, inputs)

    if image_size is None:
        return False, 'no camera image yet', {}

    if box is None or box_age_s is None or box_age_s > limit['max_age_s']:
        age = 'never' if box_age_s is None else f'{box_age_s:.1f} s ago'
        return False, (f'no fresh box of the target from track_anything (last {age}; needs {limit["max_age_s"]:g} s): '
                       'not tracking, or the button is out of the frame'), {}

    width, height        = image_size
    x_min, y_min, x_max, y_max = box
    margin_x, margin_y   = limit['margin_frac'] * width, limit['margin_frac'] * height
    edges                = {'left': x_min, 'top': y_min, 'right': width - x_max, 'bottom': height - y_max}
    margins              = {'left': margin_x, 'top': margin_y, 'right': margin_x, 'bottom': margin_y}
    close                = [side for side, distance in edges.items() if distance < margins[side]]
    outputs              = {
        'box':          [round(value, 1) for value in box],
        'image_size':   [width, height],
        'centre_frac':  [round((x_min + x_max) / 2 / width, 3), round((y_min + y_max) / 2 / height, 3)],
        'edge_px':      {side: round(distance, 1) for side, distance in edges.items()},
    }
    where = f'box centre at ({outputs["centre_frac"][0]:.2f}, {outputs["centre_frac"][1]:.2f}) of the image'

    if close:
        sides = ', '.join(f'{side} {edges[side]:.0f} px' for side in close)
        return False, f'the target is at the edge of the image ({sides}; needs {limit["margin_frac"]:.0%}): {where}', outputs

    return True, f'in view: {where}', outputs


def judge_arm(position: tuple, limits: dict | None = None, inputs: dict | None = None) -> tuple[bool, str]:
    """True for the left arm: the button sits more than left_arm_min_y_m to the robot's left; a tree sets it per panel"""
    threshold = _limits(ARM_DEFAULTS, limits, inputs)['left_arm_min_y_m']
    y         = position[1]

    if y > threshold:
        return True, f'left arm: the button is {y:+.3f} m to the side, past {threshold:+.2f}'

    return False, f'right arm: the button is {y:+.3f} m to the side, not past {threshold:+.2f}'


# ========== The skill ==========

class InViewHandle(Handle):
    """Seed the tracker with the query, then judge the first box and 3D pose that arrive after the seed"""

    def __init__(self, skill: 'ButtonInView', inputs: dict):
        super().__init__()

        interface      = skill.contract.interface
        self.skill     = skill
        self.inputs    = inputs
        self.publisher = skill.ctx.publisher(interface.name, interface.ros_type)
        self.watch     = skill.ctx.watch(interface.success_when)
        self.deadline  = time.monotonic() + skill.ctx.server_wait_s
        self.seeded    = None

    def poll(self) -> Status:
        if self.status != Status.RUNNING:
            return self.status

        now = time.monotonic()

        if self.seeded is None:
            self._seed(now)
            return self.status

        skill    = self.skill
        tracking = self.watch.matches(skill.contract.interface.success_when, after=self.seeded)
        box      = skill.tracking.box_seen
        pose     = skill.tracking.poses.get(skill.target['pose_topic'])
        has_box  = box is not None and box[0] > self.seeded
        has_pose = pose is not None and pose[0] > self.seeded

        if tracking and has_box and has_pose:
            success, message, self.outputs = judge_view(box[1], now - box[0], skill.ctx.camera.size, skill.limits, self.inputs)
            position = pose[1].pose.position
            self.outputs['pose_camera'] = [round(position.x, 3), round(position.y, 3), round(position.z, 3)]
            self.outputs['left_arm']    = False
            message += self._choose_arm(pose[1]) if success else '; right arm'
            self._finish(success, message)
        elif now - self.seeded > skill.wait_s:
            missing = [what for what, held in (('no track', tracking), ('no 2D box', has_box),
                                               ('no 3D pose: too close for depth, or no depth points', has_pose)) if not held]
            self.outputs = {'left_arm': False}
            self._finish(False, f'{skill.wait_s:g} s after seeding "{self.inputs["query"]}": {", ".join(missing)}; right arm')

        return self.status

    def _choose_arm(self, pose: PoseStamped) -> str:
        frame = self.skill.target.get('frame', 'pelvis')

        try:
            position, _ = self.skill.ctx.tf.pose_in(pose, frame)
        except Exception as error:                      # tf2 raises several kinds; all mean "not available"
            return f'; right arm: no transform {frame} <- {pose.header.frame_id} ({error})'

        left_arm, reason = judge_arm(position, self.skill.limits, self.inputs)
        self.outputs.update({'left_arm': left_arm, f'position_{frame}': [round(value, 3) for value in position]})

        return f'; {reason}'

    def _seed(self, now: float):
        interface = self.skill.contract.interface

        if self.publisher.get_subscription_count() > 0:
            message      = get_message(interface.ros_type)()
            message.data = str(self.inputs['query'])
            self.publisher.publish(message)
            self.seeded = now
        elif now > self.deadline:
            self._finish(False, f'nobody subscribes to {interface.name}')


class ButtonInView(Skill):
    """Seeds track_anything on interface.name and judges what it tracks (checks.py). config.target names the box and
    pose topics and the frame the arm is chosen in; config.limits holds the thresholds"""

    def setup(self):
        interface = self.contract.interface

        if not (interface.name and interface.success_when and self.config.get('target', {}).get('box_topic')
                and self.config['target'].get('pose_topic') and self.contract.input('query')):
            raise ValueError('button_in_view needs name (the seed topic), success_when (tracking), config.target.box_topic, '
                             'config.target.pose_topic and a query input')

        self.target   = self.config['target']
        self.limits   = self.config.get('limits') or {}
        self.wait_s   = float(self.limits.get('max_wait_s', 10.0))
        self.tracking: Tracking = tracking(self.ctx)

        self.ctx.publisher(interface.name, interface.ros_type)
        self.tracking.follow_boxes(self.target['box_topic'], self.target.get('target_topic', ''))
        self.tracking.follow_pose(self.target['pose_topic'])

    def start(self, inputs: dict) -> Handle:
        return InViewHandle(self, inputs)
