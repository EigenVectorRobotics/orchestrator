from orchestrator.common.geometry import transform_pose
from orchestrator.sensing import TfError
from orchestrator.skills.api import DoneHandle, Handle, Skill

from ..lib.press import press_state
from ..lib.tracking import tracking
from .capture import CaptureConfig, PressCapture, rotation


EE_SAMPLE_S = 0.01                      # how often the capture samples the end effector from TF


class PressButton(Skill):
    """The press action, with three camera frames kept around it for press_judge (capture.py). config.capture names the
    end-effector frames, the gripper frames whose depth fit moves the residual to the real tip, and the button's box topics"""

    def setup(self):
        capture          = self.config.get('capture') or {}
        self.capture     = CaptureConfig.from_contract(capture)
        self.press       = press_state(self.ctx)
        self.ee_frames   = {arm: capture[f'{arm}_ee_frame'] for arm in ('right', 'left') if capture.get(f'{arm}_ee_frame')}
        self.gripper_frames = {arm: capture[f'{arm}_gripper_frame'] for arm in ('right', 'left') if capture.get(f'{arm}_gripper_frame')}
        self._ee_stamp   = None             # stamp of the last TF sample fed to the capture
        self._estimate_stamp = None         # stamp of the last gripper estimate fed to the capture
        self._camera_stamp = None           # stamp of the last camera pose fed to the capture
        self.tracking    = tracking(self.ctx)

        if capture.get('box_topic'):
            self.tracking.follow_boxes(capture['box_topic'], capture.get('target_topic', ''))

        if self.ee_frames:
            self.ctx.tf.buffer()
            self.ctx.node.create_timer(EE_SAMPLE_S, self._sample_ee)

    def before(self, inputs: dict) -> Handle | None:
        if self.press.offset is None:                   # move_to_pose may hold an offset from before this executor
            return DoneHandle(False, f'{self.contract.name}: no set_press_offset since the executor started; '
                                     f'run it first (x=0 y=0 z=0 for no offset)')

        return None

    def wrap(self, handle: Handle, inputs: dict) -> Handle:
        capture                = PressCapture(self.capture, self.ctx.camera.frames, bool(inputs.get('left_arm', False)),
                                              self.tracking.boxes, self.ctx.artifacts_dir, self.press.offset)
        self.press.last_capture = capture

        handle.on_feedback.append(lambda feedback: self._on_feedback(capture, feedback))
        handle.on_end.append(lambda _: capture.on_action_end(self.ctx.now()))

        return handle

    @staticmethod
    def _on_feedback(capture: PressCapture, feedback):
        """The goal the arm is sent to, from the action feedback's current_goal"""
        goal = getattr(feedback, 'current_goal', None)

        if goal is not None and goal.header.frame_id:
            position, rotation = goal.pose.position, goal.pose.orientation
            capture.on_goal((position.x, position.y, position.z), (rotation.x, rotation.y, rotation.z, rotation.w),
                            goal.header.frame_id)

    def _sample_ee(self):
        """Feed the running capture the end effector in the goal's frame, once per new TF stamp"""
        capture = self.press.last_capture

        if capture is None or capture.ended_t is not None or not capture.goal_frame:
            return

        arm   = 'left' if capture.left_arm else 'right'
        frame = self.ee_frames.get(arm)
        self._sample_camera(capture)

        if self.gripper_frames.get(arm):
            self._sample_tip_shift(capture, self.gripper_frames[arm], frame)

        try:
            transform = self.ctx.tf.lookup(capture.goal_frame, frame)
        except TfError:
            return                                      # TF not there yet; the capture times out on its own

        stamp = transform.header.stamp.sec + transform.header.stamp.nanosec * 1e-9

        if stamp == self._ee_stamp:
            return

        self._ee_stamp = stamp
        position       = transform.transform.translation
        capture.on_pose(stamp or self.ctx.now(), (position.x, position.y, position.z))

    def _sample_camera(self, capture: PressCapture):
        """Feed the capture the camera's pose against the goal's frame, once per new TF stamp, for the aim crops"""
        if not self.ctx.camera.frame_id:
            return

        try:
            transform = self.ctx.tf.lookup(self.ctx.camera.frame_id, capture.goal_frame)
        except TfError:
            return                                      # TF not there yet: the press goes without aim crops

        stamp = transform.header.stamp.sec + transform.header.stamp.nanosec * 1e-9

        if stamp == self._camera_stamp:
            return

        self._camera_stamp = stamp
        shift, turn        = transform.transform.translation, transform.transform.rotation
        capture.on_camera_pose(stamp or self.ctx.now(), rotation((turn.x, turn.y, turn.z, turn.w)), (shift.x, shift.y, shift.z))

    def _sample_tip_shift(self, capture: PressCapture, gripper: str, ee: str):
        """Feed the capture each new depth fit of the gripper, as the fingertip's shift from the encoder fingertip at the
        fit's stamp: the fit's orientation carries its wrist pitch to the tip"""
        try:
            estimate = self.ctx.tf.lookup(capture.goal_frame, f'{gripper}_estimate')
            stamp    = estimate.header.stamp.sec + estimate.header.stamp.nanosec * 1e-9

            if stamp == self._estimate_stamp:
                return

            encoder = self.ctx.tf.lookup(capture.goal_frame, gripper, stamp=estimate.header.stamp)
            tip     = self.ctx.tf.lookup(gripper, ee).transform.translation
        except TfError:
            return                                      # no fit yet, or the encoder TF no longer holds its stamp

        self._estimate_stamp = stamp
        fitted, encoded = (transform_pose((tip.x, tip.y, tip.z), (0.0, 0.0, 0.0, 1.0),
                                          (t.transform.translation.x, t.transform.translation.y, t.transform.translation.z),
                                          (t.transform.rotation.x, t.transform.rotation.y, t.transform.rotation.z,
                                           t.transform.rotation.w))[0] for t in (estimate, encoder))

        capture.on_tip_shift(stamp, tuple(f - e for f, e in zip(fitted, encoded)))
