import json
import time

from collections import deque

from rclpy.node import Node
from rosidl_runtime_py.utilities import get_message

from skill_registry.contract import HeldValue, Watch

from ..common.topics import COMMAND_QOS, LATCHED_QOS


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
