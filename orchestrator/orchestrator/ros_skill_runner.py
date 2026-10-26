"""Run any skill from its contract over ROS 2: actions, services, topics, topic watches, and the local VLM"""

import json
import math
import time

from concurrent.futures import ThreadPoolExecutor

from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus
from sensor_msgs.msg import CompressedImage
from geometry_msgs.msg import PoseStamped
from rosidl_runtime_py import set_message_fields
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from skill_registry import Contract, Registry
from skill_registry.contract import WHOLE_MESSAGE, Watch

from .vlm_client import VlmClient, VlmClientError
from .behavior_tree import Status


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
    message.pose.orientation.z  = math.sin(yaw / 2)
    message.pose.orientation.w  = math.cos(yaw / 2)

    return message


def build_message(message_type, contract: Contract, inputs: dict, node: Node):
    """A goal, request or topic message with the fixed fields, then the input ports"""
    message = message_type()

    if contract.interface.fixed:
        set_message_fields(message, contract.interface.fixed)

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

        for parent in parents:
            target = getattr(target, parent)

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
    def __init__(self, client: ActionClient, goal, contract: Contract, server_wait_s: float):
        super().__init__(contract, server_wait_s)

        self.client      = client
        self.goal        = goal
        self.goal_handle = None
        self.cancelled   = False

    def _server_ready(self) -> bool:
        return self.client.server_is_ready()

    def _send(self):
        self.client.send_goal_async(self.goal).add_done_callback(self._on_goal_response)

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
        self._finish(success, getattr(result, 'message', '') or f'goal status {response.status}')


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


# ========== Runner ==========

class RosSkillRunner:
    def __init__(
        self,
        node: Node,
        registry: Registry,
        vlm_client: VlmClient | None = None,
        image_topic: str = '/camera/camera/color/image_raw/compressed',
        server_wait_s: float = 3.0
    ):
        self.node          = node
        self.vlm_client    = vlm_client or VlmClient()
        self.server_wait_s = server_wait_s

        self._action_clients:  dict[str, ActionClient] = {}
        self._service_clients: dict[str, object]       = {}
        self._publishers:      dict[str, object]       = {}
        self._watches:         dict[tuple, TopicWatch] = {}
        self._vlm_pool    = ThreadPoolExecutor(max_workers=2)
        self._image       = None
        self._image_topic = image_topic

        for contract in registry:
            self.prepare(contract)

        node.create_subscription(CompressedImage, image_topic, self._on_image, SENSOR_QOS)

    def prepare(self, contract: Contract):
        """Make clients and subscriptions early: discovery is done, and watches have a value, when a tree first asks"""
        interface = contract.interface

        if interface.success_when is not None:
            self._watch(interface.success_when)

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

                return _ActionHandle(self._action_client(interface.name, action_type), goal, contract, self.server_wait_s)

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

            if interface.transport == 'vlm':
                return self._ask_vlm(contract, inputs)
        except (AttributeError, ValueError, TypeError, KeyError, ModuleNotFoundError) as error:
            return _DoneHandle(False, f'cannot call {contract.name}: {error}')

        return _DoneHandle(False, f'transport {interface.transport} is not supported by this runner')

    def shutdown(self):
        self._vlm_pool.shutdown(wait=False, cancel_futures=True)

    def _check_watch(self, spec: Watch):
        watch = self._watch(spec)

        if watch.latest is None:
            return _DoneHandle(False, f'no message on {spec.topic} yet')

        return _DoneHandle(watch.matches(spec), f'{spec.field} = {watch.read(spec)}')

    def _ask_vlm(self, contract: Contract, inputs: dict):
        interface = contract.interface

        if self._image is None:
            return _DoneHandle(False, f'no camera image on {self._image_topic}')

        choices  = interface.choices or [choice.strip() for choice in inputs.get('choices', '').split(';') if choice.strip()]
        question = interface.question.format(**inputs)

        if not choices:
            return _DoneHandle(False, 'a VLM question needs choices')

        future = self._vlm_pool.submit(self.vlm_client.ask, bytes(self._image.data), question, choices)

        return _VlmHandle(future, question, interface.success_answer)

    def _on_image(self, message: CompressedImage):
        self._image = message

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
