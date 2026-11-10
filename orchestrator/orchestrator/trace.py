import json
import time

from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from rcl_interfaces.msg import Log


TRACE_TOPIC = '/orchestrator/trace'
TRACE_QOS   = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE)
LEVELS    = {'debug': Log.DEBUG, 'info': Log.INFO, 'warn': Log.WARN, 'error': Log.ERROR}


class Tracer:
    """Every planner and executor step, as JSON on /orchestrator/trace and as a line for the Foxglove Log panel"""

    def __init__(self, node: Node, source: str):
        self.node   = node
        self.source = source
        self.fields = {}            # added to every event, e.g. the run_id of the tree that runs

        self._json_publisher = node.create_publisher(String, TRACE_TOPIC, TRACE_QOS)
        self._log_publisher  = node.create_publisher(Log, '/orchestrator/log', TRACE_QOS)

    def emit(self, kind: str, text: str, level: str = 'info', **data):
        event = {'time': time.time(), 'source': self.source, 'kind': kind, 'text': text, 'level': level, **self.fields, **data}

        self._json_publisher.publish(String(data=json.dumps(event, default=str)))

        log = Log()

        log.stamp = self.node.get_clock().now().to_msg()
        log.level = LEVELS[level]
        log.name  = f'{self.source}.{kind}'
        log.msg   = text

        self._log_publisher.publish(log)
