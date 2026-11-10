"""Plan a tree for a task with an LLM, then send it to the executor"""

import json
import threading

import rclpy

from pathlib import Path

from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import String

from .trace import Tracer
from .tasks import TASK_TOPIC, parse_task_request
from .planner import Planner
from .node_setup import load_skills
from .llm_backends import ClaudeBackend, OpenAICompatibleBackend, ScriptedBackend


LIVE_QOS = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)


class PlannerNode(Node):
    def __init__(self):
        super().__init__('orchestrator_planner')

        self.registry, self.places, self.essential_devices = load_skills(self)

        self.backend_name        = self.declare_parameter('backend', 'claude').value     # claude | openai_compatible | scripted
        self.model               = self.declare_parameter('model', 'claude-opus-5').value
        self.effort              = self.declare_parameter('effort', 'high').value
        self.base_url            = self.declare_parameter('base_url', '').value          # openai_compatible only
        self.api_key_env         = self.declare_parameter('api_key_env', 'OPENAI_API_KEY').value
        self.scripted_tree       = self.declare_parameter('scripted_tree', '').value        # scripted only: a tree file
        self.max_attempts        = self.declare_parameter('max_attempts', 3).value
        self.use_live_skills     = self.declare_parameter('use_live_skills', True).value
        self.auto_run            = self.declare_parameter('auto_run', True).value

        self.tracer      = Tracer(self, 'planner')
        self.live_skills = None
        self.busy        = threading.Lock()
        self.planner     = Planner(
            self._make_backend(),
            self.registry,
            self.essential_devices,
            self.max_attempts,
            on_event=self.tracer.emit
        )

        self._tree_publisher  = self.create_publisher(String, '/orchestrator/tree', 10)
        self._blind_publisher = self.create_publisher(String, '/orchestrator/tree_blind', 10)
        self._plan_publisher  = self.create_publisher(String, '/orchestrator/plan', LIVE_QOS)
        self._planner_subscriptions = [
            self.create_subscription(String, TASK_TOPIC, self._on_task, 10),
            self.create_subscription(String, '/skills/live', self._on_live, LIVE_QOS),
        ]

        self.get_logger().info(f'planner up: {self.planner.backend.name} {self.model}, {len(self.registry)} skills')

    def _make_backend(self):
        if self.backend_name == 'claude':
            return ClaudeBackend(self.model, self.effort)

        if self.backend_name == 'openai_compatible':
            return OpenAICompatibleBackend(self.base_url, self.model, self.api_key_env)

        if self.backend_name == 'scripted':
            if not self.scripted_tree:
                raise ValueError('the scripted backend needs scripted_tree, a tree file')
            tree = Path(self.scripted_tree).read_text()
            return ScriptedBackend([f'Scripted plan from {Path(self.scripted_tree).name}.\n<tree>\n{tree}\n</tree>'])

        raise ValueError(f'unknown backend {self.backend_name}')

    def _on_live(self, message: String):
        self.live_skills = set(json.loads(message.data))

    def _on_task(self, message: String):
        task, blind = parse_task_request(message.data)

        if not self.busy.acquire(blocking=False):
            self.tracer.emit('error', 'still planning the last task; this one is dropped', level='warn')
            return

        threading.Thread(target=self._plan, args=(task, blind), daemon=True).start()

    def _plan(self, task: str, blind: bool):
        try:
            live   = self.live_skills if self.use_live_skills else None
            result = self.planner.plan(task, self.places(), live, blind)

            if not result.ok:
                return

            self._plan_publisher.publish(String(data=result.tree))

            if self.auto_run:
                publisher = self._blind_publisher if blind else self._tree_publisher
                publisher.publish(String(data=result.tree))
                self.tracer.emit('sent', 'tree sent to the executor' + (' (blind)' if blind else ''))
        finally:
            self.busy.release()


def main():
    rclpy.init()
    node = PlannerNode()

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
