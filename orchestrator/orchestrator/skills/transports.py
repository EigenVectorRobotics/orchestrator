import copy
import json
import time
import dataclasses

from typing import TYPE_CHECKING, Callable
from collections import deque

from rclpy.node import Node
from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from rosidl_runtime_py import message_to_ordereddict, set_message_fields
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from skill_registry import Contract
from skill_registry.contract import WHOLE_MESSAGE, HeldValue, Watch

from ..common.geometry import yaw_quaternion
from ..common.topics import COMMAND_QOS, LATCHED_QOS
from ..health.checks import judge_alive, run_command, url_answers
from ..tree.engine import Status
from ..vlm.client import VlmClientError
from .api import DoneHandle, FutureHandle, Handle

if TYPE_CHECKING:
    from .runtime import SkillContext


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


def read_field(message, dotted: str):
    for name in dotted.split('.'):
        message = message.get(name) if isinstance(message, dict) else getattr(message, name, None)

    return message


# ========== Topic watches ==========

WATCH_HISTORY = 10                      # the most messages a watch keeps, for Watch.consecutive


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

MAX_FEEDBACK = 1200                 # samples kept per action call; beyond it every other one is dropped


class ServerHandle(Handle):
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

    def _server_ready(self) -> bool:
        raise NotImplementedError

    def _send(self):
        raise NotImplementedError

    def _poll_sent(self) -> Status:
        return self.status


class ActionHandle(ServerHandle):
    """An action goal; its feedback is kept as samples {t: seconds since sent, ...the feedback fields}, and each
    feedback message also goes to the on_feedback listeners"""

    def __init__(self, client: ActionClient, goal, contract: Contract, server_wait_s: float):
        super().__init__(contract, server_wait_s)

        self.client      = client
        self.goal        = goal
        self.goal_handle = None
        self.cancelled   = False
        self.feedback    = []
        self.sent_time   = 0.0
        self.held        = HoldTimer(contract.interface.failure_when) if contract.interface.failure_when else None
        self.on_feedback: list[Callable[[object], None]] = []

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

        for listener in self.on_feedback:
            listener(message.feedback)

    def _poll_sent(self) -> Status:
        if self.status == Status.RUNNING and self.held is not None and self.held.expired(time.monotonic()):
            spec = self.held.spec
            self.cancel()
            self._finish(False, f'{spec.field} = {spec.equals} for {spec.hold_s:g} s')

        return self.status

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
        text         = getattr(result, 'message', '') or getattr(result, 'reason', '')     # NavigateToGoal says reason

        self._finish(success, text or f'goal status {response.status}')


class ServiceHandle(ServerHandle):
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


class TopicHandle(Handle):
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


# ========== Transports ==========

class Transport:
    """How the executor reaches what a contract names: one instance per transport, shared by every skill using it"""

    name = ''

    def prepare(self, contract: Contract, ctx: 'SkillContext'):
        """At startup: make clients early, so discovery is done when a tree first asks"""

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> Handle:
        raise NotImplementedError


class ActionTransport(Transport):
    name = 'action'

    def prepare(self, contract: Contract, ctx: 'SkillContext'):
        ctx.action_client(contract.interface.name, get_action(contract.interface.ros_type))

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> ActionHandle:
        action_type = get_action(contract.interface.ros_type)
        goal        = build_message(action_type.Goal, contract, inputs, ctx.node)

        return ActionHandle(ctx.action_client(contract.interface.name, action_type), goal, contract, ctx.server_wait_s)


class ServiceTransport(Transport):
    name = 'service'

    def prepare(self, contract: Contract, ctx: 'SkillContext'):
        ctx.service_client(contract.interface.name, get_service(contract.interface.ros_type))

        if contract.interface.success_when is not None:
            ctx.watch(contract.interface.success_when)

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> ServiceHandle:
        interface    = contract.interface
        service_type = get_service(interface.ros_type)
        request      = build_message(service_type.Request, contract, inputs, ctx.node)
        watch        = ctx.watch(interface.success_when) if interface.success_when else None

        return ServiceHandle(ctx.service_client(interface.name, service_type), request, contract, ctx.server_wait_s, watch)


class TopicTransport(Transport):
    name = 'topic'

    def prepare(self, contract: Contract, ctx: 'SkillContext'):
        ctx.publisher(contract.interface.name, contract.interface.ros_type)

        if contract.interface.success_when is not None:
            ctx.watch(contract.interface.success_when)

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> TopicHandle:
        interface = contract.interface
        message   = build_message(get_message(interface.ros_type), contract, inputs, ctx.node)
        watch     = ctx.watch(interface.success_when) if interface.success_when else None

        return TopicHandle(ctx.publisher(interface.name, interface.ros_type), message, contract, watch, ctx.server_wait_s)


class WatchTransport(Transport):
    """A condition on the latest messages of a topic; it never publishes"""

    name = 'watch'

    def prepare(self, contract: Contract, ctx: 'SkillContext'):
        ctx.watch(contract.interface.success_when)

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> DoneHandle:
        return check_watch(contract.interface.success_when, inputs, ctx)


def check_watch(spec: Watch, inputs: dict, ctx: 'SkillContext') -> DoneHandle:
    if spec.equals_input:
        spec = dataclasses.replace(spec, equals=inputs[spec.equals_input])

    watch = ctx.watch(spec)

    if watch.latest is None:
        return DoneHandle(False, f'no message on {spec.topic} yet')

    age = time.monotonic() - watch.receive_time

    if spec.max_age_s and age > spec.max_age_s:
        return DoneHandle(False, f'no message on {spec.topic} for {age:.1f} s (needs one within {spec.max_age_s:g} s)')

    return DoneHandle(watch.matches(spec), f'{spec.field} = {watch.read(spec)}')


class CompareTransport(Transport):
    """The input value equals the input equals; nothing leaves the executor"""

    name = 'compare'

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> DoneHandle:
        value, equals = str(inputs['value']), str(inputs['equals'])

        return DoneHandle(value == equals, f'{value} {"==" if value == equals else "!="} {equals}')


class AliveTransport(Transport):
    """Each listed topic has a publisher, each service exists, each URL answers"""

    name = 'alive'

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> FutureHandle:
        return FutureHandle(ctx.submit('commands', check_alive, ctx.node, contract.interface.alive))


class CommandTransport(Transport):
    """The contract's fixed shell command on the executor's machine; exit code 0 is success"""

    name = 'command'

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> FutureHandle:
        return FutureHandle(ctx.submit('commands', run_command, contract.interface.command, contract.timeout_s or 60.0))


def check_alive(node, spec: dict) -> tuple[bool, str, dict]:
    published = {name for name, _ in node.get_topic_names_and_types() if node.count_publishers(name) > 0}
    services  = {name for name, _ in node.get_service_names_and_types()}

    return judge_alive(spec, published, services, url_answers)


class VlmHandle(Handle):
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


class VlmTransport(Transport):
    """A closed question about the latest camera frame, to the VLM server"""

    name = 'vlm'

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> Handle:
        interface = contract.interface

        if ctx.camera.jpeg is None:
            return DoneHandle(False, f'no camera image on {ctx.camera.topic}')

        choices  = interface.choices or [choice.strip() for choice in inputs.get('choices', '').split(';') if choice.strip()]
        question = interface.question.format(**inputs)

        if not choices:
            return DoneHandle(False, 'a VLM question needs choices')

        future = ctx.submit('vlm', ctx.vlm_client.ask, [ctx.camera.jpeg], question, choices)

        return VlmHandle(future, question, interface.success_answer)


class PythonTransport(Transport):
    """A skill whose skill.py runs every call itself; reaching here means it did not"""

    name = 'python'

    def start(self, contract: Contract, inputs: dict, ctx: 'SkillContext') -> DoneHandle:
        return DoneHandle(False, f'{contract.name}: transport python needs a skill.py whose start() runs the call')


def default_transports() -> dict[str, Transport]:
    """Every built-in transport by name; agent and tree are run by their own leaves"""
    transports = [ActionTransport(), ServiceTransport(), TopicTransport(), WatchTransport(), CompareTransport(),
                  AliveTransport(), CommandTransport(), VlmTransport(), PythonTransport()]

    return {transport.name: transport for transport in transports}
