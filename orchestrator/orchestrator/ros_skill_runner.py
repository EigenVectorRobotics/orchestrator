"""Run any skill from its contract over ROS 2: actions, services, topics, topic watches, and the local VLM"""

import copy
import json
import math
import time

from concurrent.futures import ThreadPoolExecutor

from rclpy.node import Node
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
from .press_capture import DEFAULT_CAPTURE_DIR, BoxBuffer, CaptureConfig, FrameBuffer, PressCapture, judge_press, stamp_seconds


MAX_FEEDBACK = 1200                 # samples kept per action call; beyond it every other one is dropped

# The fake robot and the elevator_sim bridge use the same profiles; keep them in step.
COMMAND_QOS = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
LATCHED_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
SENSOR_QOS  = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)


# ========== Messages ==========

def to_pose_stamped(pose: dict, node: Node) -> PoseStamped:
    message = PoseStamped()
    yaw     = float(pose.get('yaw', 0.0))

    message.header.frame_id     = pose.get('frame', 'map')
    message.header.stamp        = node.get_clock().now().to_msg()
    message.pose.position.x     = float(pose['x'])
    message.pose.position.y     = float(pose['y'])
    message.pose.position.z     = float(pose.get('z', 0.0))     # the storey on a multi-storey map; 0 = the lowest
    message.pose.orientation.z  = math.sin(yaw / 2)
    message.pose.orientation.w  = math.cos(yaw / 2)

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
    """The latest message on one topic; JSON payloads are parsed once per message"""

    def __init__(self, node: Node, topic: str, ros_type: str, latched: bool):
        self.latest       = None
        self.receive_time = 0.0
        self._parsed      = None
        self.subscription = node.create_subscription(
            get_message(ros_type),
            topic,
            self._on_message,
            LATCHED_QOS if latched else COMMAND_QOS
        )

    def read(self, spec: Watch):
        """The watched field of the latest message, or None"""
        if self.latest is None:
            return None

        if spec.json:
            if self._parsed is None:
                try:
                    self._parsed = json.loads(self.latest.data)
                except (json.JSONDecodeError, TypeError):
                    self._parsed = {}

            value = self._parsed
        else:
            value = self.latest

        for key in spec.field.split('.'):
            value = value.get(key) if isinstance(value, dict) else getattr(value, key, None)

        return value

    def matches(self, spec: Watch, after: float = 0.0) -> bool:
        return self.latest is not None and self.receive_time > after and self.read(spec) == spec.equals

    def _on_message(self, message):
        self.latest, self._parsed, self.receive_time = message, None, time.monotonic()


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
            self.capture.on_goal((position.x, position.y, position.z), (rotation.x, rotation.y, rotation.z, rotation.w))

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
    def __init__(self, client, request, contract: Contract, server_wait_s: float):
        super().__init__(contract, server_wait_s)

        self.client  = client
        self.request = request
        self.future  = None

    def _server_ready(self) -> bool:
        return self.client.service_is_ready()

    def _send(self):
        self.future = self.client.call_async(self.request)

    def _poll_sent(self) -> Status:
        if self.future.done():
            response     = self.future.result()
            self.outputs = extract_outputs(self.contract, response)
            results      = getattr(response, 'results', None)          # rcl_interfaces SetParameters: one per parameter

            if results is not None and all(hasattr(result, 'successful') for result in results):
                failed = [result.reason for result in results if not result.successful]
                self._finish(not failed, '; '.join(failed) or f'{len(results)} parameters set')
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


class _PressCheckHandle(_Handle):
    """A VLM check on the frames a press kept: waits until they are chosen, then asks about each later frame
    beside the baseline. Lit in either is success"""

    def __init__(self, runner: 'RosSkillRunner', capture: PressCapture, contract: Contract, inputs: dict):
        super().__init__()

        self.runner   = runner
        self.capture  = capture
        self.contract = contract
        self.inputs   = inputs
        self.future   = None

    def poll(self) -> Status:
        if self.status != Status.RUNNING:
            return self.status

        if self.future is None:
            if self.capture.ready(self.runner.now()):
                self.future = self.runner._vlm_pool.submit(self._judge)

            return self.status

        if not self.future.done():
            return self.status

        try:
            success, message, self.outputs = self.future.result()
        except VlmClientError as error:
            self.outputs = {'capture': self.capture.events(), 'frames': self.capture.saved}
            success, message = False, str(error)

        self._finish(success, message)

        return self.status

    def _judge(self) -> tuple[bool, str, dict]:
        latest = self.runner.latest_image()

        return judge_press(self.capture, self.contract.interface, self.inputs, self.runner.vlm_client.ask,
                           latest, self.runner.capture_dir)


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
        self.last_capture  = None           # the frames of the last press, for its check and the agent
        self._pose_topics  = set()
        self._box_topics   = set()
        self._target_id    = None

        self._action_clients:  dict[str, ActionClient] = {}
        self._service_clients: dict[str, object]       = {}
        self._publishers:      dict[str, object]       = {}
        self._watches:         dict[tuple, TopicWatch] = {}
        self._vlm_pool    = ThreadPoolExecutor(max_workers=2)
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
            topic = interface.capture.get(f'{arm}_pose_topic')

            if topic and topic not in self._pose_topics:
                self._pose_topics.add(topic)
                self.node.create_subscription(PoseStamped, topic, lambda message, arm=arm: self._on_ee_pose(arm, message), COMMAND_QOS)

        if interface.capture.get('box_topic') and interface.capture['box_topic'] not in self._box_topics:
            self._subscribe_boxes(interface.capture['box_topic'], interface.capture.get('target_topic', ''))

        try:
            if interface.transport == 'action':
                self._action_client(interface.name, get_action(interface.ros_type))
            elif interface.transport == 'service':
                self._service_client(interface.name, get_service(interface.ros_type))
            elif interface.transport == 'topic':
                self._publisher(interface.name, interface.ros_type)
        except (AttributeError, ValueError, ModuleNotFoundError) as error:
            self.node.get_logger().warn(f'{contract.name}: {error}; it fails if a tree uses it')

    def start(self, contract: Contract, inputs: dict):
        interface = contract.interface

        try:
            if interface.transport == 'action':
                action_type = get_action(interface.ros_type)
                goal        = build_message(action_type.Goal, contract, inputs, self.node)

                capture = None

                if interface.capture:
                    capture           = PressCapture(CaptureConfig.from_contract(interface.capture), self.frames,
                                                     bool(inputs.get('left_arm', False)), self.boxes)
                    self.last_capture = capture

                return _ActionHandle(self._action_client(interface.name, action_type), goal, contract, self.server_wait_s, capture, self.now)

            if interface.transport == 'service':
                service_type = get_service(interface.ros_type)
                request      = build_message(service_type.Request, contract, inputs, self.node)

                return _ServiceHandle(self._service_client(interface.name, service_type), request, contract, self.server_wait_s)

            if interface.transport == 'watch':
                return self._check_watch(interface.success_when)

            if interface.transport == 'topic':
                message = build_message(get_message(interface.ros_type), contract, inputs, self.node)
                watch   = self._watch(interface.success_when) if interface.success_when else None

                return _TopicHandle(self._publisher(interface.name, interface.ros_type), message, contract, watch, self.server_wait_s)

            if interface.transport == 'graph':
                return self._check_graph(interface.topics)

            if interface.transport == 'press_tip':
                return self._check_press_tip(interface.limits)

            if interface.transport == 'vlm':
                return self._ask_vlm(contract, inputs)

            if interface.transport == 'compare':
                value, equals = str(inputs['value']), str(inputs['equals'])
                return _DoneHandle(value == equals, f'{value} {"==" if value == equals else "!="} {equals}')
        except (AttributeError, ValueError, TypeError, KeyError, ModuleNotFoundError) as error:
            return _DoneHandle(False, f'cannot call {contract.name}: {error}')

        return _DoneHandle(False, f'transport {interface.transport} is not supported by this runner')

    def _check_graph(self, topics: dict):
        """Each topic has no more publishers than allowed, not counting the executor's own: a leftover process would fight the stack's"""
        extra = []

        for topic, allowed in topics.items():
            own   = (self.node.get_name(), self.node.get_namespace())
            names = [f'{info.node_namespace.rstrip("/")}/{info.node_name}' for info in self.node.get_publishers_info_by_topic(topic)
                     if (info.node_name, info.node_namespace) != own]

            if len(names) > int(allowed):
                extra.append(f'{topic}: {len(names)} publishers {sorted(names)}')

        return _DoneHandle(not extra, '; '.join(extra) if extra else f'{len(topics)} topics with their own publishers only')

    def _check_press_tip(self, limits: dict):
        """Where the end effector got to in the last press: deep enough, and close enough to the goal sideways"""
        capture = self.last_capture

        if capture is None or capture.tracker.lateral_at_max is None:
            return _DoneHandle(False, 'no press with end-effector poses to judge')

        depth, lateral = capture.tracker.max_depth, capture.tracker.lateral_at_max
        min_depth, max_lateral = float(limits.get('min_depth_m', -0.012)), float(limits.get('max_lateral_m', 0.008))
        reached = depth >= min_depth and lateral <= max_lateral

        return _DoneHandle(reached, f'deepest {depth * 1000:+.1f} mm along the button normal (needs >= {min_depth * 1000:+.1f}), '
                                    f'{lateral * 1000:.1f} mm sideways there (needs <= {max_lateral * 1000:.1f})')

    def shutdown(self):
        self._vlm_pool.shutdown(wait=False, cancel_futures=True)

    def latest_image(self) -> bytes | None:
        """The last camera frame, as JPEG: what the VLM sees"""
        return self._image

    def _check_watch(self, spec: Watch):
        watch = self._watch(spec)

        if watch.latest is None:
            return _DoneHandle(False, f'no message on {spec.topic} yet')

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
            'capture':  capture.events(),
            'frames':   capture.save(self.capture_dir),
        }

    def capture_frames(self) -> dict:
        """The frames the last press kept, by name"""
        return {name: data for name, (_, data) in self.last_capture.frames.items()} if self.last_capture else {}

    def _ask_vlm(self, contract: Contract, inputs: dict):
        interface = contract.interface

        if interface.frames == 'press_capture' and self.last_capture is not None:
            return _PressCheckHandle(self, self.last_capture, contract, inputs)

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

        stamp       = stamp_seconds(message.header.stamp)
        self._image = jpeg
        self.frames.add(stamp or self.now(), jpeg)

    def _subscribe_boxes(self, box_topic: str, target_topic: str):
        """track_anything's 2D boxes (stamped as their camera frame) and the id of the target it selected"""
        try:
            from vision_msgs.msg import Detection2DArray, Detection3D
        except ImportError as error:
            self.node.get_logger().warn(f'no vision_msgs ({error}): button_lit judges whole frames')
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
        stamp       = stamp_seconds(message.header.stamp)

        self.boxes.add(stamp or self.now(), (centre.x - box.size_x / 2, centre.y - box.size_y / 2,
                                             centre.x + box.size_x / 2, centre.y + box.size_y / 2))

    def _on_ee_pose(self, arm: str, message: PoseStamped):
        capture = self.last_capture

        if capture is None or capture.left_arm != (arm == 'left') or capture.ended_t is not None:
            return

        stamp    = stamp_seconds(message.header.stamp)
        position = message.pose.position
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
