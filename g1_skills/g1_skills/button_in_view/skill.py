import time

from geometry_msgs.msg import PoseStamped
from rosidl_runtime_py.utilities import get_message

from orchestrator.skills.api import Handle, Skill
from orchestrator.tree.engine import Status

from ..lib.tracking import Tracking, tracking
from .checks import judge_arm, judge_view


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
