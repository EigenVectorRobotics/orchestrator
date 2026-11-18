import time

from typing import Callable

from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus
from rosidl_runtime_py import message_to_ordereddict

from skill_registry import Contract

from ..tree.engine import Status
from .api import Handle
from .watch import HoldTimer, TopicWatch
from .messages import extract_outputs, read_field


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
