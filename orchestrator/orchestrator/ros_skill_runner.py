"""Run any skill from its contract over ROS 2: actions, services, topics, topic watches, and the local VLM"""

import copy
import dataclasses
import json
import math
import time
import threading

from collections import deque

from concurrent.futures import ThreadPoolExecutor

from rclpy.node import Node
from rclpy.time import Time
from rclpy.duration import Duration
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus
from sensor_msgs.msg import Image
from geometry_msgs.msg import PoseStamped
from rosidl_runtime_py import message_to_ordereddict, set_message_fields
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from skill_registry import Contract, Registry
from skill_registry.contract import WHOLE_MESSAGE, HeldValue, Watch

from .images import ImageError, image_to_jpeg
from .vlm_client import VlmClient, VlmClientError
from .behavior_tree import Status
from .press_capture import DEFAULT_CAPTURE_DIR, BoxBuffer, CaptureConfig, FrameBuffer, PressCapture
from .stance_checks import judge_arm, judge_view
from .geometry import rotate, transform_pose, yaw_quaternion
from .preflight_checks import judge_alive, run_command, url_answers


PRESS_OFFSET_SKILL = 'set_press_offset'      # its inputs move move_to_pose's pre-grasp, which the press capture watches for


MAX_FEEDBACK = 1200                 # samples kept per action call; beyond it every other one is dropped

# The fake robot and the elevator_sim bridge use the same profiles; keep them in step.
COMMAND_QOS = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
LATCHED_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
WATCH_HISTORY = 10                      # the most messages a watch keeps, for Watch.consecutive
SENSOR_QOS  = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
EE_SAMPLE_S = 0.01                      # how often a press capture samples the end effector from TF


# ========== Messages ==========

def to_pose_stamped(pose: dict, node: Node) -> PoseStamped:
    message = PoseStamped()
    yaw     = float(pose.get('yaw', 0.0))

    message.header.frame_id     = pose.get('frame', 'map')
    message.header.stamp        = node.get_clock().now().to_msg()
    message.pose.position.x     = float(pose['x'])
    message.pose.position.y     = float(pose['y'])
    message.pose.position.z     = float(pose.get('z', 0.0))     # the storey on a multi-storey map; 0 = the lowest
    _, _, message.pose.orientation.z, message.pose.orientation.w = yaw_quaternion(yaw)

    return message


def build_message(message_type, contract: Contract, inputs: dict, node: Node):
    """A goal, request or topic message with the fixed fields, then the input ports"""
    message = message_type()

    if contract.interface.fixed:      # a copy: set_message_fields turns a list of dicts into messages in place
        set_message_fields(message, copy.deepcopy(contract.interface.fixed))

    for port in contract.inputs:
        if port.name not in inputs:
            continue

        value = inputs[port.name]

        if port.type == 'pose':
            value = to_pose_stamped(value, node)
        elif port.type == 'enum' and port.values:
            value = port.values[value]
        elif port.type == 'float':
            value = float(value)

        if port.message_field == WHOLE_MESSAGE:
            message = value
            continue

        *parents, leaf = port.message_field.split('.')
        target          = message

        for parent in parents:             # a number is an index into a sequence, e.g. parameters.0.value
            target = target[int(parent)] if parent.isdigit() else getattr(target, parent)

        setattr(target, leaf, value)

    return message


def extract_outputs(contract: Contract, result) -> dict:
    return {port.name: getattr(result, port.message_field, None) for port in contract.outputs}


# ========== Topic watches ==========

class TopicWatch:
    """The latest messages on one topic; JSON payloads are parsed once per message"""

    def __init__(self, node: Node, topic: str, ros_type: str, latched: bool):
        self.latest       = None
        self.receive_time = 0.0
        self.history      = deque(maxlen=WATCH_HISTORY)   # (message, parsed JSON or None), oldest first
        self.subscription = node.create_subscription(
            get_message(ros_type),
            topic,
            self._on_message,
            LATCHED_QOS if latched else COMMAND_QOS
        )

    def read(self, spec: Watch, back: int = 0):
        """The watched field of the latest message (back 0) or of an earlier one, or None"""
        if back >= len(self.history):
            return None

        index           = len(self.history) - 1 - back
        message, parsed = self.history[index]

        if spec.json:
            if parsed is None:
                try:
                    parsed = json.loads(message.data)
                except (json.JSONDecodeError, TypeError):
                    parsed = {}

                self.history[index] = (message, parsed)

            value = parsed
        else:
            value = message

        for key in spec.field.split('.'):
            value = value.get(key) if isinstance(value, dict) else getattr(value, key, None)

        return value

    def matches(self, spec: Watch, after: float = 0.0) -> bool:
        """The last spec.consecutive messages all hold spec.equals, and the latest came after the time given"""
        if self.latest is None or self.receive_time <= after or len(self.history) < spec.consecutive:
            return False

        return all(self.read(spec, back) == spec.equals for back in range(spec.consecutive))

    def _on_message(self, message):
        self.latest, self.receive_time = message, time.monotonic()
        self.history.append((message, None))


# ========== Held values ==========

def read_field(message, dotted: str):
    for name in dotted.split('.'):
        message = message.get(name) if isinstance(message, dict) else getattr(message, name, None)

    return message


class HoldTimer:
    """How long a value has equalled spec.equals without a break; expired once that reaches spec.hold_s"""

    def __init__(self, spec: HeldValue):
        self.spec  = spec
        self.since = None

    def update(self, value, now: float):
        if value == self.spec.equals:
            self.since = self.since if self.since is not None else now
        else:
            self.since = None

    def expired(self, now: float) -> bool:
        return self.since is not None and now - self.since >= self.spec.hold_s


# ========== Handles ==========

class _Handle:
    def __init__(self):
        self.status  = Status.RUNNING
        self.message = ''
        self.outputs = {}

    def poll(self) -> Status:
        return self.status

    def cancel(self):
        pass

    def _finish(self, success: bool, message: str = ''):
        self.status  = Status.SUCCESS if success else Status.FAILURE
        self.message = message


class _DoneHandle(_Handle):
    def __init__(self, success: bool, message: str = '', outputs: dict | None = None):
        super().__init__()

        self._finish(success, message)
        self.outputs = outputs or {}


class _ServerHandle(_Handle):
    """Waits up to server_wait_s for the server, then sends once; subclasses say how"""

    def __init__(self, contract: Contract, server_wait_s: float):
        super().__init__()

        self.contract = contract
        self.deadline = time.monotonic() + server_wait_s
        self.sent     = False

    def poll(self) -> Status:
        if self.status != Status.RUNNING:
            return self.status

        if self.sent:
            return self._poll_sent()

        if self._server_ready():
            self.sent = True
            self._send()
        elif time.monotonic() > self.deadline:
            self._finish(False, f'no server for {self.contract.interface.name}')

        return self.status

    def _poll_sent(self) -> Status:
        return self.status


class _ActionHandle(_ServerHandle):
    """An action goal; its feedback is kept as samples {t: seconds since sent, ...the feedback fields}.

    With a capture, the feedback's current_goal is passed on to it, and the end of the call is told to it.
    """

    def __init__(self, client: ActionClient, goal, contract: Contract, server_wait_s: float,
                 capture: PressCapture | None = None, now=None):
        super().__init__(contract, server_wait_s)

        self.client      = client
        self.goal        = goal
        self.goal_handle = None
        self.cancelled   = False
        self.feedback    = []
        self.sent_time   = 0.0
        self.capture     = capture
        self.now         = now
        self.held        = HoldTimer(contract.interface.failure_when) if contract.interface.failure_when else None

    def _server_ready(self) -> bool:
        return self.client.server_is_ready()

    def _send(self):
        self.sent_time = time.monotonic()
        self.client.send_goal_async(self.goal, feedback_callback=self._on_feedback).add_done_callback(self._on_goal_response)

    def _on_feedback(self, message):
        sample = {'t': round(time.monotonic() - self.sent_time, 2), **message_to_ordereddict(message.feedback)}

        self.feedback.append(sample)

        if len(self.feedback) > MAX_FEEDBACK:
            self.feedback = self.feedback[::2]

        if self.held is not None:
            self.held.update(read_field(message.feedback, self.held.spec.field), time.monotonic())

        goal = getattr(message.feedback, 'current_goal', None)

        if self.capture is not None and goal is not None and goal.header.frame_id:
            position, rotation = goal.pose.position, goal.pose.orientation
            self.capture.on_goal((position.x, position.y, position.z), (rotation.x, rotation.y, rotation.z, rotation.w),
                                 goal.header.frame_id)

    def _poll_sent(self) -> Status:
        if self.status == Status.RUNNING and self.held is not None and self.held.expired(time.monotonic()):
            spec = self.held.spec
            self.cancel()
            self._finish(False, f'{spec.field} = {spec.equals} for {spec.hold_s:g} s')

        return self.status

    def _finish(self, success: bool, message: str = ''):
        if self.status != Status.RUNNING:
            return                                      # already ended, e.g. by failure_when; the late result changes nothing

        super()._finish(success, message)

        if self.capture is not None:
            self.capture.on_action_end(self.now())

    def cancel(self):
        self.cancelled = True

        if self.goal_handle is not None:
            self.goal_handle.cancel_goal_async()

    def _on_goal_response(self, future):
        self.goal_handle = future.result()

        if not self.goal_handle.accepted:
            self._finish(False, 'goal rejected')
            return

        if self.cancelled:
            self.goal_handle.cancel_goal_async()
            return

        self.goal_handle.get_result_async().add_done_callback(self._on_result)

    def _on_result(self, future):
        response = future.result()
        result   = response.result
        success  = response.status == GoalStatus.STATUS_SUCCEEDED and getattr(result, 'success', True)

        self.outputs = extract_outputs(self.contract, result)
        text     = getattr(result, 'message', '') or getattr(result, 'reason', '')     # NavigateToGoal says reason

        self._finish(success, text or f'goal status {response.status}')


class _ServiceHandle(_ServerHandle):
    """A service call; with a watch, a successful response is done only once success_when holds on a later message"""

    def __init__(self, client, request, contract: Contract, server_wait_s: float, watch: TopicWatch | None = None):
        super().__init__(contract, server_wait_s)

        self.client        = client
        self.request       = request
        self.future        = None
        self.watch         = watch
        self.response_time = None
        self.response_text = ''

    def _server_ready(self) -> bool:
        return self.client.service_is_ready()

    def _send(self):
        self.future = self.client.call_async(self.request)

    def _poll_sent(self) -> Status:
        if self.response_time is not None:
            if self.watch.matches(self.contract.interface.success_when, after=self.response_time):
                self._finish(True, self.response_text)

            return self.status

        if self.future.done():
            response     = self.future.result()
            self.outputs = extract_outputs(self.contract, response)
            results      = getattr(response, 'results', None)          # rcl_interfaces SetParameters: one per parameter

            if results is not None and all(hasattr(result, 'successful') for result in results):
                failed = [result.reason for result in results if not result.successful]
                self._finish(not failed, '; '.join(failed) or f'{len(results)} parameters set')
            elif getattr(response, 'success', True) and self.watch is not None:
                self.response_time = time.monotonic()
                self.response_text = getattr(response, 'message', '')
            else:
                self._finish(getattr(response, 'success', True), getattr(response, 'message', ''))

        return self.status


class _TopicHandle(_Handle):
    """Publish once a subscriber is there; done when success_when holds on a later message, or at once if there is none"""

    def __init__(self, publisher, message, contract: Contract, watch: TopicWatch | None, server_wait_s: float):
        super().__init__()

        self.publisher    = publisher
        self.outgoing     = message
        self.contract     = contract
        self.watch        = watch
        self.deadline     = time.monotonic() + server_wait_s
        self.publish_time = None

    def poll(self) -> Status:
        if self.status != Status.RUNNING:
            return self.status

        if self.publish_time is None:
            if self.publisher.get_subscription_count() > 0:
                self.publisher.publish(self.outgoing)
                self.publish_time = time.monotonic()

                if self.watch is None:
                    self._finish(True)
            elif time.monotonic() > self.deadline:
                self._finish(False, f'nobody subscribes to {self.contract.interface.name}')

            return self.status

        if self.watch.matches(self.contract.interface.success_when, after=self.publish_time):
            self._finish(True)

        return self.status


class _FutureHandle(_Handle):
    """A check or command run in a thread: the future gives (success, message, outputs)"""

    def __init__(self, future):
        super().__init__()

        self.future = future

    def poll(self) -> Status:
        if self.status != Status.RUNNING or not self.future.done():
            return self.status

        try:
            success, message, self.outputs = self.future.result()
        except Exception as error:                      # a crash in the thread is a failed skill, not a dead executor
            success, message = False, f'{type(error).__name__}: {error}'

        self._finish(success, message)

        return self.status


class _VlmHandle(_Handle):
    def __init__(self, future, question: str, success_answer: str):
        super().__init__()

        self.future         = future
        self.question       = question
        self.success_answer = success_answer

    def poll(self) -> Status:
        if self.status != Status.RUNNING or not self.future.done():
            return self.status

        try:
            answer = self.future.result()
        except VlmClientError as error:
            self._finish(False, str(error))
            return self.status

        self.outputs = {'answer': answer}
        self._finish(not self.success_answer or answer == self.success_answer, f'{self.question} -> {answer}')

        return self.status


class _ReachHandle(_ServiceHandle):
    """The latest target pose, moved by the press offset in force, sent to the IK check service; done when it answers,
    successful when in reach. ik_server keeps its launch offset (0), so the offset set_press_offset gave move_to_pose is added here"""

    def __init__(self, runner: 'RosSkillRunner', contract: Contract, inputs: dict):
        interface = contract.interface
        service   = get_service(interface.ros_type)
        pose      = runner._targets.get(interface.target['pose_topic'])
        max_age   = float(interface.limits.get('max_age_s', 2.0))
        stale     = pose is None or time.monotonic() - pose[0] > max_age
        request   = None
        offset    = runner.press_offset or (0.0, 0.0, 0.0)   # no set_press_offset yet (a look runs before the first press): 0

        if not stale:
            target                   = copy.deepcopy(pose[1])
            turn                     = target.pose.orientation
            shift                    = rotate((turn.x, turn.y, turn.z, turn.w), offset)
            target.pose.position.x  += shift[0]
            target.pose.position.y  += shift[1]
            target.pose.position.z  += shift[2]
            request                  = service.Request()
            request.target_pose      = target
            request.left_arm         = bool(inputs.get('left_arm', False))

        super().__init__(runner._service_client(interface.name, service), request, contract, runner.server_wait_s)
        self.offset = offset

        if stale:
            age = 'none yet' if pose is None else f'{time.monotonic() - pose[0]:.1f} s old'
            self._finish(False, f'no target pose on {interface.target["pose_topic"]} within {max_age:g} s ({age}): look first')

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


class _InViewHandle(_Handle):
    """Seed the tracker with the query, then judge the first box and 3D pose that arrive after the seed"""

    def __init__(self, runner: 'RosSkillRunner', contract: Contract, inputs: dict):
        super().__init__()

        interface      = contract.interface
        self.runner    = runner
        self.contract  = contract
        self.inputs    = inputs
        self.publisher = runner._publisher(interface.name, interface.ros_type)
        self.watch     = runner._watch(interface.success_when)
        self.wait_s    = float(interface.limits.get('max_wait_s', 10.0))
        self.deadline  = time.monotonic() + runner.server_wait_s
        self.seeded    = None

    def poll(self) -> Status:
        if self.status != Status.RUNNING:
            return self.status

        now = time.monotonic()

        if self.seeded is None:
            self._seed(now)
            return self.status

        interface = self.contract.interface
        tracking  = self.watch.matches(interface.success_when, after=self.seeded)
        box       = self.runner._box_seen
        pose      = self.runner._targets.get(interface.target['pose_topic'])
        has_box   = box is not None and box[0] > self.seeded
        has_pose  = pose is not None and pose[0] > self.seeded

        if tracking and has_box and has_pose:
            success, message, self.outputs = judge_view(box[1], now - box[0], self.runner._image_size, interface.limits, self.inputs)
            position = pose[1].pose.position
            self.outputs['pose_camera'] = [round(position.x, 3), round(position.y, 3), round(position.z, 3)]
            self.outputs['left_arm']    = False
            message += self._choose_arm(pose[1]) if success else '; right arm'
            self._finish(success, message)
        elif now - self.seeded > self.wait_s:
            missing = [what for what, held in (('no track', tracking), ('no 2D box', has_box),
                                               ('no 3D pose: too close for depth, or no depth points', has_pose)) if not held]
            self.outputs = {'left_arm': False}
            self._finish(False, f'{self.wait_s:g} s after seeding "{self.inputs["query"]}": {", ".join(missing)}; right arm')

        return self.status

    def _choose_arm(self, pose: PoseStamped) -> str:
        frame = self.contract.interface.target.get('frame', 'pelvis')

        try:
            position, _ = self.runner._pose_in(pose, frame)
        except Exception as error:                      # tf2 raises several kinds; all mean "not available"
            return f'; right arm: no transform {frame} <- {pose.header.frame_id} ({error})'

        left_arm, reason = judge_arm(position, self.contract.interface.limits, self.inputs)
        self.outputs.update({'left_arm': left_arm, f'position_{frame}': [round(value, 3) for value in position]})

        return f'; {reason}'

    def _seed(self, now: float):
        if self.publisher.get_subscription_count() > 0:
            message      = get_message(self.contract.interface.ros_type)()
            message.data = str(self.inputs['query'])
            self.publisher.publish(message)
            self.seeded = now
        elif now > self.deadline:
            self._finish(False, f'nobody subscribes to {self.contract.interface.name}')


# ========== Runner ==========

class RosSkillRunner:
    def __init__(
        self,
        node: Node,
        registry: Registry,
        vlm_client: VlmClient | None = None,
        image_topic: str = '/camera/camera/color/image_raw',
        server_wait_s: float = 3.0,
        capture_dir: str = DEFAULT_CAPTURE_DIR
    ):
        self.node          = node
        self.vlm_client    = vlm_client or VlmClient()
        self.server_wait_s = server_wait_s
        self.capture_dir   = capture_dir
        self.frames        = FrameBuffer()
        self.boxes         = BoxBuffer()    # the target button's box per frame, from track_anything
        self.last_capture  = None           # the frames of the last press, for the agent
        self.press_offset  = None             # the last set_press_offset, which moves move_to_pose's pre-grasp; None: not set yet
        self._ee_frames    = {}             # arm -> end-effector TF frame, from the capture contracts
        self._ee_timer     = None
        self._ee_stamp     = None           # stamp of the last TF sample fed to the capture
        self._box_topics   = set()
        self._target_id    = None
        self._box_seen     = None           # (monotonic receive time, box) of track_anything's latest target box
        self._image_size   = None           # (width, height) of the last camera frame
        self._targets      = {}             # in_view pose topic -> (monotonic receive time, PoseStamped)
        self._target_topics = set()
        self._tf_buffer    = None
        self._tf_lock      = threading.Lock()   # the agent's thread reads TF through this buffer too (RosReader)

        self._action_clients:  dict[str, ActionClient] = {}
        self._service_clients: dict[str, object]       = {}
        self._publishers:      dict[str, object]       = {}
        self._watches:         dict[tuple, TopicWatch] = {}
        self._vlm_pool    = ThreadPoolExecutor(max_workers=2)
        self._command_pool = ThreadPoolExecutor(max_workers=2)      # alive checks and stack launches
        self._image       = None             # the last camera frame, as JPEG
        self._image_topic = image_topic

        for contract in registry:
            self.prepare(contract)

        node.create_subscription(Image, image_topic, self._on_image, SENSOR_QOS)

    def prepare(self, contract: Contract):
        """Make clients and subscriptions early: discovery is done, and watches have a value, when a tree first asks"""
        interface = contract.interface

        if interface.success_when is not None:
            self._watch(interface.success_when)

        for arm in ('right', 'left'):
            if interface.capture.get(f'{arm}_ee_frame'):
                self._ee_frames[arm] = interface.capture[f'{arm}_ee_frame']

        if self._ee_frames and self._ee_timer is None:
            self._tf()
            self._ee_timer = self.node.create_timer(EE_SAMPLE_S, self._sample_ee)

        for source in (interface.capture, interface.target):
            if source.get('box_topic') and source['box_topic'] not in self._box_topics:
                self._subscribe_boxes(source['box_topic'], source.get('target_topic', ''))

        if interface.target.get('pose_topic'):
            self._subscribe_target(interface.target['pose_topic'])

        try:
            if interface.transport == 'action':
                self._action_client(interface.name, get_action(interface.ros_type))
            elif interface.transport == 'service':
                self._service_client(interface.name, get_service(interface.ros_type))
            elif interface.transport == 'reach':
                self._service_client(interface.name, get_service(interface.ros_type))
            elif interface.transport in ('topic', 'in_view'):
                self._publisher(interface.name, interface.ros_type)
        except (AttributeError, ValueError, ModuleNotFoundError) as error:
            self.node.get_logger().warn(f'{contract.name}: {error}; it fails if a tree uses it')

    def start(self, contract: Contract, inputs: dict):
        interface = contract.interface

        if contract.name == PRESS_OFFSET_SKILL:          # move_to_pose keeps it until the next one; the capture needs it
            self.press_offset = tuple(float(inputs.get(axis, 0.0)) for axis in 'xyz')

        if interface.capture and self.press_offset is None:     # move_to_pose may hold an offset from before this executor
            return _DoneHandle(False, f'{contract.name}: no {PRESS_OFFSET_SKILL} since the executor started; '
                                      f'run it first (x=0 y=0 z=0 for no offset)')

        try:
            if interface.transport == 'action':
                action_type = get_action(interface.ros_type)
                goal        = build_message(action_type.Goal, contract, inputs, self.node)

                capture = None

                if interface.capture:
                    capture           = PressCapture(CaptureConfig.from_contract(interface.capture), self.frames,
                                                     bool(inputs.get('left_arm', False)), self.boxes, self.capture_dir,
                                                     self.press_offset)
                    self.last_capture = capture

                return _ActionHandle(self._action_client(interface.name, action_type), goal, contract, self.server_wait_s, capture, self.now)

            if interface.transport == 'service':
                service_type = get_service(interface.ros_type)
                request      = build_message(service_type.Request, contract, inputs, self.node)
                watch        = self._watch(interface.success_when) if interface.success_when else None

                return _ServiceHandle(self._service_client(interface.name, service_type), request, contract, self.server_wait_s, watch)

            if interface.transport == 'watch':
                return self._check_watch(interface.success_when, inputs)

            if interface.transport == 'topic':
                message = build_message(get_message(interface.ros_type), contract, inputs, self.node)
                watch   = self._watch(interface.success_when) if interface.success_when else None

                return _TopicHandle(self._publisher(interface.name, interface.ros_type), message, contract, watch, self.server_wait_s)

            if interface.transport == 'in_view':
                return _InViewHandle(self, contract, inputs)

            if interface.transport == 'reach':
                return _ReachHandle(self, contract, inputs)

            if interface.transport == 'alive':
                return _FutureHandle(self._command_pool.submit(self._check_alive, interface.alive))

            if interface.transport == 'command':
                return _FutureHandle(self._command_pool.submit(run_command, interface.command, contract.timeout_s or 60.0))

            if interface.transport == 'vlm':
                return self._ask_vlm(contract, inputs)

            if interface.transport == 'compare':
                value, equals = str(inputs['value']), str(inputs['equals'])
                return _DoneHandle(value == equals, f'{value} {"==" if value == equals else "!="} {equals}')
        except (AttributeError, ValueError, TypeError, KeyError, ModuleNotFoundError) as error:
            return _DoneHandle(False, f'cannot call {contract.name}: {error}')

        return _DoneHandle(False, f'transport {interface.transport} is not supported by this runner')

    def _pose_in(self, message: PoseStamped, frame: str) -> tuple[tuple, tuple]:
        """The stamped pose's position and rotation in frame, through the latest TF; raises when TF has no transform"""
        pose        = message.pose
        transform   = self._tf().lookup_transform(frame, message.header.frame_id, Time())
        shift, turn = transform.transform.translation, transform.transform.rotation

        return transform_pose(
            (pose.position.x, pose.position.y, pose.position.z),
            (pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w),
            (shift.x, shift.y, shift.z), (turn.x, turn.y, turn.z, turn.w)
        )

    def _subscribe_target(self, topic: str):
        if topic in self._target_topics:
            return

        self._target_topics.add(topic)
        self.node.create_subscription(PoseStamped, topic, lambda message: self._targets.__setitem__(topic, (time.monotonic(), message)), COMMAND_QOS)
        self._tf()                                      # listen from the start, so TF is there at the first check

    def _tf(self):
        """The executor's one TF buffer and listener, made on first use; the runner and the agent's RosReader share it"""
        with self._tf_lock:
            if self._tf_buffer is None:
                from tf2_ros import Buffer, TransformListener

                self._tf_buffer   = Buffer(cache_time=Duration(seconds=10))
                self._tf_listener = TransformListener(self._tf_buffer, self.node)

            return self._tf_buffer

    def _check_alive(self, spec: dict) -> tuple[bool, str, dict]:
        """The listed topics have publishers, the services exist, the URLs answer"""
        published = {name for name, _ in self.node.get_topic_names_and_types() if self.node.count_publishers(name) > 0}
        services  = {name for name, _ in self.node.get_service_names_and_types()}

        return judge_alive(spec, published, services, url_answers)

    def shutdown(self):
        self._vlm_pool.shutdown(wait=False, cancel_futures=True)
        self._command_pool.shutdown(wait=False, cancel_futures=True)

    def latest_image(self) -> bytes | None:
        """The last camera frame, as JPEG: what the VLM sees"""
        return self._image

    def _check_watch(self, spec: Watch, inputs: dict):
        if spec.equals_input:
            spec = dataclasses.replace(spec, equals=inputs[spec.equals_input])

        watch = self._watch(spec)

        if watch.latest is None:
            return _DoneHandle(False, f'no message on {spec.topic} yet')

        age = time.monotonic() - watch.receive_time

        if spec.max_age_s and age > spec.max_age_s:
            return _DoneHandle(False, f'no message on {spec.topic} for {age:.1f} s (needs one within {spec.max_age_s:g} s)')

        return _DoneHandle(watch.matches(spec), f'{spec.field} = {watch.read(spec)}')

    def now(self) -> float:
        return self.node.get_clock().now().nanoseconds * 1e-9

    def press_evidence(self, wait_s: float = 8.0) -> dict | None:
        """The last press for the agent's judge phase: once its frames are chosen (or wait_s has passed), save them
        and return their paths with the phase times, depth and sideways distance"""
        capture = self.last_capture

        if capture is None:
            return None

        deadline = time.monotonic() + wait_s

        while not capture.ready(self.now()) and time.monotonic() < deadline:
            time.sleep(0.1)

        tracker = capture.tracker

        return {
            'left_arm': capture.left_arm,
            'goal':     None if tracker.goal is None else [round(float(value), 4) for value in tracker.goal],
            'press_offset_m': [round(float(value), 4) for value in capture.press_offset],   # the offset this press used
            'capture':  capture.events(),
            'frames':   capture.save(self.capture_dir),
        }

    def _ask_vlm(self, contract: Contract, inputs: dict):
        interface = contract.interface

        if self._image is None:
            return _DoneHandle(False, f'no camera image on {self._image_topic}')

        choices  = interface.choices or [choice.strip() for choice in inputs.get('choices', '').split(';') if choice.strip()]
        question = interface.question.format(**inputs)

        if not choices:
            return _DoneHandle(False, 'a VLM question needs choices')

        future = self._vlm_pool.submit(self.vlm_client.ask, self._image, question, choices)

        return _VlmHandle(future, question, interface.success_answer)

    def _on_image(self, message: Image):
        """Raw frames from the RealSense, encoded here: the Orin publishes no compressed transport"""
        try:
            jpeg = image_to_jpeg(message)
        except ImageError as error:
            self.node.get_logger().warn(f'camera frame dropped: {error}', throttle_duration_sec=10.0)
            return

        stamp       = message.header.stamp.sec + message.header.stamp.nanosec * 1e-9
        self._image = jpeg
        self._image_size = (message.width, message.height)
        self.frames.add(stamp or self.now(), jpeg)

    def _subscribe_boxes(self, box_topic: str, target_topic: str):
        """track_anything's 2D boxes (stamped as their camera frame) and the id of the target it selected"""
        try:
            from vision_msgs.msg import Detection2DArray, Detection3D
        except ImportError as error:
            self.node.get_logger().warn(f'no vision_msgs ({error}): press frames are kept without crops')
            return

        self._box_topics.add(box_topic)
        self.node.create_subscription(Detection2DArray, box_topic, self._on_boxes, COMMAND_QOS)

        if target_topic:
            self.node.create_subscription(Detection3D, target_topic, lambda message: setattr(self, '_target_id', message.id), COMMAND_QOS)

    def _on_boxes(self, message):
        if not message.detections:
            return

        detection = next((item for item in message.detections if item.id == self._target_id), message.detections[0])
        centre, box = detection.bbox.center.position, detection.bbox
        stamp       = message.header.stamp.sec + message.header.stamp.nanosec * 1e-9

        corners = (centre.x - box.size_x / 2, centre.y - box.size_y / 2, centre.x + box.size_x / 2, centre.y + box.size_y / 2)

        self.boxes.add(stamp or self.now(), corners)
        self._box_seen = (time.monotonic(), corners)    # by receive time: the Orin's stamps need not match this clock

    def _sample_ee(self):
        """Feed the running press capture the end effector in the goal's frame, once per new TF stamp"""
        capture = self.last_capture

        if capture is None or capture.ended_t is not None or not capture.goal_frame:
            return

        frame = self._ee_frames.get('left' if capture.left_arm else 'right')

        try:
            transform = self._tf().lookup_transform(capture.goal_frame, frame, Time())
        except Exception:
            return                                      # TF not there yet; the capture times out on its own

        stamp = transform.header.stamp.sec + transform.header.stamp.nanosec * 1e-9

        if stamp == self._ee_stamp:
            return

        self._ee_stamp = stamp
        position       = transform.transform.translation
        capture.on_pose(stamp or self.now(), (position.x, position.y, position.z))

    def _watch(self, spec: Watch) -> TopicWatch:
        key = (spec.topic, spec.latched)

        if key not in self._watches:
            self._watches[key] = TopicWatch(self.node, spec.topic, spec.ros_type, spec.latched)

        return self._watches[key]

    def _action_client(self, name: str, action_type) -> ActionClient:
        if name not in self._action_clients:
            self._action_clients[name] = ActionClient(self.node, action_type, name)

        return self._action_clients[name]

    def _service_client(self, name: str, service_type):
        if name not in self._service_clients:
            self._service_clients[name] = self.node.create_client(service_type, name)

        return self._service_clients[name]

    def _publisher(self, name: str, ros_type: str):
        if name not in self._publishers:
            self._publishers[name] = self.node.create_publisher(get_message(ros_type), name, COMMAND_QOS)

        return self._publishers[name]
