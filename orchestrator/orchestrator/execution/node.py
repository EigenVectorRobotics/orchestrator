"""Validate a behavior tree, then tick it on the NUC; every step goes to the trace"""

import json

import rclpy

from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import Empty, String

from ..agent.link import PushAgentBackend, PushLink
from ..skills.runtime import SkillRunner, SkillRunnerConfig
from .node_setup import Tracer, load_skills
from .tree_runner import TreeRunner
from ..common.topics import (AGENT_EVENT_TOPIC, AGENT_REPLY_TOPIC as REPLY_TOPIC, AGENT_REQUEST_TOPIC as REQUEST_TOPIC, CANCEL_TOPIC, COMMAND_QOS,
                     LATCHED_QOS as STATUS_QOS, RECORDER_STATUS_TOPIC as RECORDER_TOPIC, SPEAK_TOPIC, STATUS_TOPIC,
                     TREE_BLIND_TOPIC, TREE_TOPIC)


def tree_message(data: str) -> tuple[str, bool]:
    """A tree message: the XML, or JSON {"tree": XML, "skip_preflight": true} from send_tree --skip-preflight"""
    if data.lstrip().startswith('{'):
        try:
            message = json.loads(data)
            return str(message['tree']), bool(message.get('skip_preflight', False))
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    return data, False


class ExecutorNode(Node):
    """The executor's ROS side: its parameters and topics, mapped onto the TreeRunner that runs trees and the
    SkillRunner that runs skills"""

    def __init__(self):
        super().__init__('orchestrator_executor')

        # (1) Skills: the runner owns everything skills share, the agent included
        registry, places, essential_devices, library = load_skills(self)
        self.tracer = Tracer(self, 'executor')
        speaker     = self.create_publisher(String, SPEAK_TOPIC, 10)                  # unitree_interface's TTS (remapped ~/tts)
        speak       = lambda text: speaker.publish(String(data=text))
        config      = SkillRunnerConfig.from_node(self)
        self.runner = SkillRunner(self, registry, config, library, agent_backend=self._make_agent_backend(speak, config.artifacts_dir),
                                  on_agent_event=self._on_agent_event, speak=speak)

        # (2) Tree runs, gated on the recorder and the preflight
        self.recorder_status = {'ready': False, 'reason': 'no status from run_recorder yet'}
        self.tree_runner     = TreeRunner(
            registry, places, essential_devices, self.runner, self.tracer, self._publish_status, self.get_logger(),
            require_recorder=self.declare_parameter('require_recorder', False).value,
            recorder_ready=self._recorder_ready,
            require_preflight=self.declare_parameter('require_preflight', False).value
        )

        # (3) Topics in and out, and the tick
        tick_hz = self.declare_parameter('tick_hz', 10.0).value
        self._status_publisher   = self.create_publisher(String, STATUS_TOPIC, STATUS_QOS)
        self._tree_subscriptions = [
            self.create_subscription(String, TREE_TOPIC, lambda message: self._on_tree(message, blind=False), 10),
            self.create_subscription(String, TREE_BLIND_TOPIC, lambda message: self._on_tree(message, blind=True), 10),
            self.create_subscription(Empty, CANCEL_TOPIC, lambda message: self.tree_runner.cancel(), 10),
            self.create_subscription(String, RECORDER_TOPIC, self._on_recorder_status, STATUS_QOS),
        ]
        self._tick_timer         = self.create_timer(1.0 / tick_hz, self.tree_runner.tick)

        self._publish_status('idle', None)
        self.get_logger().info(f'{len(registry)} skills, {len(places)} places')

    # ========== Agent backend ==========

    def _make_agent_backend(self, speak, capture_dir: str) -> PushAgentBackend | None:
        """The backend of agent skills, or None if agent_backend is none or unknown"""
        backend_name = self.declare_parameter('agent_backend', 'push').value            # push | none

        if backend_name == 'none':
            return None

        if backend_name != 'push':                                  # agent skills then fail
            self.get_logger().warn(f'no agent: unknown agent_backend {backend_name}')
            return None

        if not self.declare_parameter('agent_push_host', '').value:   # the site's agent_bridge, or agent_push_host:=
            self.get_logger().warn('no agent: agent_push_host is not set (launch with site:=<name>); agent skills fail')
            return None

        return self._make_push_backend(speak, capture_dir)

    def _on_link_state(self, text: str):
        """The agent bridge link connected or dropped: in the trace, and in the log, where the robot scripts read it"""
        self.tracer.emit('agent_link', text)
        self.get_logger().info(text)

    def _make_push_backend(self, speak, capture_dir: str) -> PushAgentBackend:
        """Every agent call goes to the agent bridge on the laptop over one TCP link; the request, reply and event topics carry it for the recorder"""
        host              = self.get_parameter('agent_push_host').value                          # the operator laptop, from the site
        port              = self.declare_parameter('agent_push_port', 8771).value
        request_publisher = self.create_publisher(String, REQUEST_TOPIC, STATUS_QOS)
        reply_publisher   = self.create_publisher(String, REPLY_TOPIC, COMMAND_QOS)
        event_publisher   = self.create_publisher(String, AGENT_EVENT_TOPIC, COMMAND_QOS)
        link              = PushLink(host, port, on_state=self._on_link_state)

        return PushAgentBackend(
            link,
            lambda request: request_publisher.publish(String(data=json.dumps(request, default=str))),
            lambda reply: reply_publisher.publish(String(data=json.dumps(reply))),
            lambda event: event_publisher.publish(String(data=json.dumps(event, default=str))),
            capture_dir,
            speak,
        )

    def _on_agent_event(self, kind: str, text: str, level: str = 'info', **data):
        self.tracer.emit(kind, text, level=level, **data)

    # ========== Topics ==========

    def _on_tree(self, message: String, blind: bool):
        xml_text, skip = tree_message(message.data)
        self.tree_runner.run(xml_text, blind=blind, skip=skip)

    def _on_recorder_status(self, message: String):
        self.recorder_status = json.loads(message.data)

    def _recorder_ready(self) -> tuple[bool, str]:
        if self.count_publishers(RECORDER_TOPIC) == 0:      # a latched ready outlives a recorder that died
            return False, 'run_recorder is not running'

        return bool(self.recorder_status.get('ready')), self.recorder_status.get('reason', '')

    def _publish_status(self, state: str, tree_id: str | None):
        self._status_publisher.publish(String(data=json.dumps({'state': state, 'tree': tree_id})))


def main():
    rclpy.init()
    node = ExecutorNode()

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.tree_runner.halt()
        node.runner.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
