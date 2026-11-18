import copy
import math
import time

from rosidl_runtime_py.utilities import get_service

from orchestrator.common.geometry import rotate
from orchestrator.skills.api import Handle, Skill
from orchestrator.skills.handles import ServiceHandle
from orchestrator.tree.engine import Status

from ..lib.press import press_state
from ..lib.tracking import tracking


class ReachHandle(ServiceHandle):
    """The latest target pose, moved by the press offset in force, sent to the IK check service; done when it answers,
    successful when in reach. ik_server keeps its launch offset (0), so the offset set_press_offset gave move_to_pose is added here"""

    def __init__(self, client, service, contract, server_wait_s: float, pose, max_age_s: float, pose_topic: str,
                 offset: tuple, left_arm: bool):
        stale   = pose is None or time.monotonic() - pose[0] > max_age_s
        request = None

        if not stale:
            target                  = copy.deepcopy(pose[1])
            turn                    = target.pose.orientation
            shift                   = rotate((turn.x, turn.y, turn.z, turn.w), offset)
            target.pose.position.x += shift[0]
            target.pose.position.y += shift[1]
            target.pose.position.z += shift[2]
            request                 = service.Request()
            request.target_pose     = target
            request.left_arm        = left_arm

        super().__init__(client, request, contract, server_wait_s)
        self.offset = offset

        if stale:
            age = 'none yet' if pose is None else f'{time.monotonic() - pose[0]:.1f} s old'
            self._finish(False, f'no target pose on {pose_topic} within {max_age_s:g} s ({age}): look first')

    def _poll_sent(self) -> Status:
        if not self.future.done():
            return self.status

        response = self.future.result()
        arm      = 'left' if self.request.left_arm else 'right'

        if not math.isfinite(response.position_error):  # no plan: the message says why
            self._finish(False, f'{arm} arm: {response.message}')
            return self.status

        # (1) The miss, and the target without the press offset: the same point as press_button's current_goal, in pelvis
        pelvis       = response.position_error_pelvis
        placed       = response.target_in_pelvis.pose
        turn         = placed.orientation
        shift        = rotate((turn.x, turn.y, turn.z, turn.w), self.offset)
        target       = (placed.position.x - shift[0], placed.position.y - shift[1], placed.position.z - shift[2])
        self.outputs = {
            'position_error':        round(response.position_error, 4),
            'orientation_error_deg': round(math.degrees(response.orientation_error), 2),
            **{f'pelvis_error_{axis}': round(getattr(pelvis, axis), 4) for axis in 'xyz'},
            **{f'target_pelvis_{axis}': round(value, 4) for axis, value in zip('xyz', target)},
        }

        # (2) The verdict, with the offset it checked
        reach = 'in reach' if response.reachable else 'target out of reach'
        self._finish(response.reachable,
                     f'{arm} arm, {reach}: the arm ends {response.position_error * 1000:.1f} mm and '
                     f'{math.degrees(response.orientation_error):.1f} deg from it; miss in the pelvis frame (x forward, '
                     f'y left, z up) x {pelvis.x * 1000:+.1f} y {pelvis.y * 1000:+.1f} z {pelvis.z * 1000:+.1f} mm; '
                     f'target in pelvis {[round(value, 3) for value in target]} m, press offset {list(self.offset)} m')

        return self.status


class CheckReach(Skill):
    """Asks ik_server whether the arm reaches the tracked button from here, without moving it. config.target.pose_topic
    is the pose press_button plans to; config.limits.max_age_s how fresh it must be"""

    def setup(self):
        self.service    = get_service(self.contract.interface.ros_type)
        self.pose_topic = self.config['target']['pose_topic']
        self.max_age_s  = float((self.config.get('limits') or {}).get('max_age_s', 2.0))
        self.tracking   = tracking(self.ctx)

        self.ctx.service_client(self.contract.interface.name, self.service)
        self.tracking.follow_pose(self.pose_topic)

    def start(self, inputs: dict) -> Handle:
        offset = press_state(self.ctx).offset or (0.0, 0.0, 0.0)    # no set_press_offset yet (a look runs before the first press): 0

        return ReachHandle(self.ctx.service_client(self.contract.interface.name, self.service), self.service, self.contract,
                           self.ctx.server_wait_s, self.tracking.poses.get(self.pose_topic), self.max_age_s,
                           self.pose_topic, offset, bool(inputs.get('left_arm', False)))
