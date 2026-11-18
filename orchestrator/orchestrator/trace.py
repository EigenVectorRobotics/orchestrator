import json
import time

from typing import Callable
from contextlib import contextmanager

from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from rcl_interfaces.msg import Log


TRACE_TOPIC = '/orchestrator/trace'
TRACE_QOS   = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE)
LEVELS    = {'debug': Log.DEBUG, 'info': Log.INFO, 'warn': Log.WARN, 'error': Log.ERROR}


class Tracer:
    """Every executor step, as JSON on /orchestrator/trace and as a line for the Foxglove Log panel"""

    def __init__(self, node: Node, source: str):
        self.node      = node
        self.source    = source
        self.fields    = {}         # added to every event, e.g. the run_id of the tree that runs
        self.listeners = []         # a tap reads the events while it is open; see tap()

        self._json_publisher = node.create_publisher(String, TRACE_TOPIC, TRACE_QOS)
        self._log_publisher  = node.create_publisher(Log, '/orchestrator/log', TRACE_QOS)

    @contextmanager
    def tap(self, listener: Callable[[dict], None]):
        """Read the events of one piece of work while it runs. The task desk uses it to send the agent what the
        robot logged, which is the only way the agent can see an event (e.g. why a step did not validate)"""
        self.listeners.append(listener)

        try:
            yield
        finally:
            self.listeners.remove(listener)

    def emit(self, kind: str, text: str, level: str = 'info', **data):
        event = {'time': time.time(), 'source': self.source, 'kind': kind, 'text': text, 'level': level, **self.fields, **data}

        self._json_publisher.publish(String(data=json.dumps(event, default=str)))

        log = Log()

        log.stamp = self.node.get_clock().now().to_msg()
        log.level = LEVELS[level]
        log.name  = f'{self.source}.{kind}'
        log.msg   = text

        self._log_publisher.publish(log)
        self._notify(event)

    def _notify(self, event: dict):
        """The taps, after the topic and the log, and never into the emitter: a tap only reads, so a listener that
        raises must not stop the work that traced the event"""
        for listener in tuple(self.listeners):
            try:
                listener(event)
            except Exception:                       # noqa: BLE001 - a broken tap costs a prompt line, not a step
                pass
