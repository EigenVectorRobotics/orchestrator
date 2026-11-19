"""Validate a behavior tree, then tick it on the NUC; every step goes to the trace"""

import json
import os

import rclpy

from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import Empty, String

from skill_registry import TreeValidator, ValidatorConfig

from ..agent.link import PushAgentBackend, PushLink
from ..skills.runtime import SkillRunner, SkillRunnerConfig
from ..tree.engine import Status, TreeError
from ..tree.leaf import make_leaf_factory
from ..tree.runner import TreeRunner
from .node_setup import Tracer, load_place_map, load_skills
from ..common.topics import (AGENT_EVENT_TOPIC, AGENT_REPLY_TOPIC as REPLY_TOPIC, AGENT_REQUEST_TOPIC as REQUEST_TOPIC, CANCEL_TOPIC, COMMAND_QOS,
                     LATCHED_QOS as STATUS_QOS, RECORDER_STATUS_TOPIC as RECORDER_TOPIC, SPEAK_TOPIC, STATUS_TOPIC,
                     TREE_BLIND_TOPIC, TREE_TOPIC)


PREFLIGHT_TREE = 'preflight'        # the ID of skill_registry/preflight/preflight.xml


def tree_message(data: str) -> tuple[str, bool]:
    """A tree message: the XML, or JSON {"tree": XML, "skip_preflight": true} from send_tree --skip-preflight"""
    if data.lstrip().startswith('{'):
        try:
            message = json.loads(data)
            return str(message['tree']), bool(message.get('skip_preflight', False))
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    return data, False


def preflight_gate(tree_id: str, passed: bool, skip: bool, required: bool) -> tuple[bool, str]:
    """Whether a tree may run, and what to trace: a task tree needs a preflight that passed since the launch, unless
    preflight is not required (require_preflight false) or the sender skips it on purpose"""
    if tree_id == PREFLIGHT_TREE or not required or passed:
        return True, ''

    if skip:
        return True, f'{tree_id} runs without a passed preflight (skip_preflight)'

    return False, (f'tree refused: no preflight has passed since the launch. Send the task to the planner, which runs it '
                   f'first, or send_tree --skip-preflight to run {tree_id} without it')


def recorder_refusal(required: bool, recorder_up: bool, status: dict) -> str:
    """Why a tree may not run unrecorded, or empty; a latched ready outlives a recorder that died, hence recorder_up"""
    if not required or (recorder_up and status.get('ready')):
        return ''

    reason = status.get('reason') if recorder_up else 'run_recorder is not running'

    return f'tree refused: the recorder is not ready ({reason})'


class ExecutorNode(Node):
    """The executor: decides which trees may run (the recorder gate, validation, the preflight gate) and maps its
    topics onto the TreeRunner that runs them and the SkillRunner that runs skills"""

    def __init__(self):
        super().__init__('orchestrator_executor')

        # (1) Skills: the runner owns everything skills share, the agent included
        self.registry, places, self.essential_devices, library = load_skills(self)
        os.environ['MAP'] = load_place_map(self.get_parameter('places_file').value)   # launch_nuc_stack starts nav on it
        self.known_keys = set(places)
        self.tracer     = Tracer(self, 'executor')
        speaker         = self.create_publisher(String, SPEAK_TOPIC, 10)              # unitree_interface's TTS (remapped ~/tts)
        speak           = lambda text: speaker.publish(String(data=text))
        config          = SkillRunnerConfig.from_node(self)
        self.runner     = SkillRunner(self, self.registry, config, library, agent_backend=self._make_agent_backend(speak, config.artifacts_dir),
                                      on_agent_event=self._on_agent_event, speak=speak)

        # (2) Which trees may run, and the runner of the one that does
        self.require_recorder  = self.declare_parameter('require_recorder', False).value   # true: no tree runs unrecorded
        self.require_preflight = self.declare_parameter('require_preflight', False).value  # true: no task tree before a passed preflight
        self.preflight_passed  = False      # the last preflight run since the launch succeeded
        self.recorder_status   = {'ready': False, 'reason': 'no status from run_recorder yet'}
        self.tree_runner       = TreeRunner(make_leaf_factory(self.registry, self.runner), self.tracer, self._publish_status,
                                            self.get_logger(), blackboard=places, on_finished=self._on_finished)

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
        self.get_logger().info(f'{len(self.registry)} skills, {len(places)} places')

    # ========== Which trees run ==========

    def run(self, xml_text: str, blind: bool = False, skip: bool = False) -> bool:
        """Start a tree once it passes the recorder gate, validation and the preflight gate; refuse it otherwise"""
        recorder_up = self.count_publishers(RECORDER_TOPIC) > 0

        if refusal := (recorder_refusal(self.require_recorder, recorder_up, self.recorder_status)
                       or self._validation_refusal(xml_text, blind)):
            return self._reject(refusal)

        try:
            tree = self.tree_runner.build(xml_text)
        except (TreeError, ValueError) as error:
            return self._reject(f'tree does not build: {error}')

        allowed, note = preflight_gate(tree.id, self.preflight_passed, skip, self.require_preflight)

        if not allowed:
            return self._reject(note, tree_id=tree.id)

        if tree.id == PREFLIGHT_TREE:
            self.preflight_passed = False   # a preflight that does not succeed closes the gate again

        self.tree_runner.start(tree, ' (blind)' if blind else '', tree=xml_text, blind=blind)

        if note:
            self.tracer.emit('preflight_skipped', note, level='warn', tree_id=tree.id)

        return True

    def _validation_refusal(self, xml_text: str, blind: bool) -> str:
        """Trace every issue but the maturity warnings; the refusal when there are errors"""
        config = ValidatorConfig(essential_devices=self.essential_devices, blind=blind, known_keys=self.known_keys)
        issues = TreeValidator(self.registry, config).validate(xml_text)
        errors = [issue for issue in issues if issue.level == 'error']

        for issue in issues:
            if issue.code == 'maturity':
                continue                                # every skill is wip for now: not worth a trace line

            self.tracer.emit('validation', f'{issue.path}: {issue.message}', level='error' if issue.level == 'error' else 'warn')

        return f'tree rejected: {len(errors)} errors' if errors else ''

    def _reject(self, text: str, **data) -> bool:
        running = self.tree_runner.tree

        self.tracer.emit('rejected', text, level='error', **data)
        self._publish_status('rejected', running.id if running else None)

        return False

    def _on_finished(self, tree_id: str, status: Status):
        if tree_id == PREFLIGHT_TREE:
            self.preflight_passed = status == Status.SUCCESS

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
        self.run(xml_text, blind=blind, skip=skip)

    def _on_recorder_status(self, message: String):
        self.recorder_status = json.loads(message.data)

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
