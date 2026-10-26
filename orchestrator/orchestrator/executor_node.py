"""Validate a behavior tree, then tick it on the NUC; every step goes to the trace"""

import json

import rclpy

from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import Empty, String

from skill_registry import TreeValidator, ValidatorConfig

from .trace import Tracer
from .node_setup import load_skills
from .vlm_client import VlmClient, VlmClientConfig
from .skill_leaf import SkillLeaf, make_leaf_factory
from .behavior_tree import BehaviorTree, Context, Status, TreeError
from .ros_skill_runner import RosSkillRunner


STATUS_QOS = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)


class ExecutorNode(Node):
    def __init__(self):
        super().__init__('orchestrator_executor')

        self.registry, self.places, self.essential_devices = load_skills(self)

        self.tick_hz      = self.declare_parameter('tick_hz', 10.0).value
        self.image_topic  = self.declare_parameter('image_topic', '/camera/camera/color/image_raw/compressed').value
        self.vlm_base_url = self.declare_parameter('vlm_base_url', 'http://vector-thor:8090/v1').value
        self.vlm_model    = self.declare_parameter('vlm_model', 'local').value

        self.tracer   = Tracer(self, 'executor')
        self.runner   = RosSkillRunner(
            self,
            self.registry,
            VlmClient(VlmClientConfig(base_url=self.vlm_base_url, model=self.vlm_model)),
            self.image_topic
        )

        self.tree = None

        self._status_publisher   = self.create_publisher(String, '/orchestrator/status', STATUS_QOS)
        self._tree_subscriptions = [
            self.create_subscription(String, '/orchestrator/tree', self._on_tree, 10),
            self.create_subscription(String, '/orchestrator/tree_blind', self._on_blind_tree, 10),
            self.create_subscription(Empty, '/orchestrator/cancel', self._on_cancel, 10),
        ]
        self._tick_timer         = self.create_timer(1.0 / self.tick_hz, self._tick)

        self._publish_status('idle')
        self.get_logger().info(f'{len(self.registry)} skills, {len(self.places)} places')

    # ========== Trees in ==========

    def _on_tree(self, message: String):
        self.run(message.data, blind=False)

    def _on_blind_tree(self, message: String):
        self.run(message.data, blind=True)

    def run(self, xml_text: str, blind: bool) -> bool:
        validator = TreeValidator(
            self.registry,
            ValidatorConfig(essential_devices=self.essential_devices, blind=blind)
        )
        issues = validator.validate(xml_text)
        errors = [issue for issue in issues if issue.level == 'error']

        for issue in issues:
            if issue.code == 'maturity':
                continue                                # every skill is wip for now: not worth a trace line

            self.tracer.emit('validation', f'{issue.path}: {issue.message}', level='error' if issue.level == 'error' else 'warn')

        if errors:
            self.tracer.emit('rejected', f'tree rejected: {len(errors)} errors', level='error')
            self._publish_status('rejected')
            return False

        if self.tree is not None:
            self.tree.halt()
            self.tracer.emit('halted', f'halted {self.tree.id} for a new tree', level='warn')

        context = Context(blackboard=dict(self.places), on_event=self._on_node_event)

        try:
            self.tree = BehaviorTree(xml_text, make_leaf_factory(self.registry, self.runner), context)
        except (TreeError, ValueError) as error:
            self.tracer.emit('rejected', f'tree does not build: {error}', level='error')
            self.tree = None
            self._publish_status('rejected')
            return False

        self.tracer.emit('started', f'running {self.tree.id}' + (' (blind)' if blind else ''), tree=xml_text)
        self._publish_status('running')

        return True

    def _on_cancel(self, message: Empty):
        if self.tree is None:
            return

        self.tree.halt()
        self.tracer.emit('halted', f'{self.tree.id} cancelled', level='warn')
        self.tree = None
        self._publish_status('cancelled')

    # ========== Ticks ==========

    def _tick(self):
        if self.tree is None:
            return

        try:
            status = self.tree.tick()
        except Exception as error:                      # a skill or runner bug must not take the executor down
            self.tree.halt()
            self.tracer.emit('error', f'{self.tree.id} stopped by an internal error: {error!r}', level='error')
            self.get_logger().error(f'tick failed: {error!r}')
            self._publish_status('error')
            self.tree = None
            return

        if status == Status.RUNNING:
            return

        level = 'info' if status == Status.SUCCESS else 'error'
        self.tracer.emit('finished', f'{self.tree.id}: {status.value}', level=level, status=status.value)
        self._publish_status(status.value)
        self.tree = None

    def _on_node_event(self, node, event: dict):
        if event['status'] == Status.IDLE.value:
            return

        skill  = isinstance(node, SkillLeaf)
        failed = event['status'] == Status.FAILURE.value

        if not skill and not (failed and event['message']):
            return                                      # a control node matters only when it fails for its own reason

        text = f'{event["node"]} {event["status"]}'

        if skill and event['status'] == Status.RUNNING.value and node.inputs:
            text += ' ' + node.describe_inputs()

        if event['message']:
            text += f': {event["message"]}'

        self.tracer.emit('node', text, level='warn' if failed else 'info', **event)

    def _publish_status(self, state: str):
        payload = {'state': state, 'tree': self.tree.id if self.tree else None}

        self._status_publisher.publish(String(data=json.dumps(payload)))


def main():
    rclpy.init()
    node = ExecutorNode()

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node.tree is not None:
            node.tree.halt()

        node.runner.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
