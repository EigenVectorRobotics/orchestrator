"""Validate a behavior tree, then tick it on the NUC; every step goes to the trace"""

import json
import uuid

from pathlib import Path

import rclpy

from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import Empty, String
from ament_index_python.packages import get_package_share_directory

from .agent import Agent, AgentConfig
from .agent_backends import OpenAICompatibleAgentBackend, ScriptedAgentBackend
from .agent_external import DECISION_TOPIC, REPLY_TOPIC, REQUEST_TOPIC, ExternalAgentBackend
from .trace import Tracer
from .agent_ros import RosReader
from .agent_sandbox import SandboxConfig
from .knowledge import KnowledgeStore
from .node_setup import load_skills
from .vlm_client import VlmClient, VlmClientConfig
from .skill_leaf import SkillLeaf, make_leaf_factory
from .tasks import Tasks
from .behavior_tree import Context, Status
from .ros_skill_runner import RosSkillRunner
from .press_capture import DEFAULT_CAPTURE_DIR
from .run_recorder import STATUS_TOPIC as RECORDER_TOPIC


STATUS_QOS  = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
COMMAND_QOS = QoSProfile(depth=5)


class ExecutorNode(Node):
    def __init__(self):
        super().__init__('orchestrator_executor')

        self.registry, self.places, self.essential_devices = load_skills(self)

        self.tick_hz      = self.declare_parameter('tick_hz', 10.0).value
        self.image_topic  = self.declare_parameter('image_topic', '/camera/camera/color/image_raw').value
        self.vlm_base_url = self.declare_parameter('vlm_base_url', 'http://vector-thor:8090/v1').value
        self.vlm_model    = self.declare_parameter('vlm_model', 'local').value
        self.vlm_timeout  = self.declare_parameter('vlm_timeout_s', 40.0).value    # Cosmos on the Thor: one request at a time
        self.capture_dir  = self.declare_parameter('capture_dir', DEFAULT_CAPTURE_DIR).value
        self.require_recorder = self.declare_parameter('require_recorder', False).value   # true: no tree runs unrecorded
        self.recorder_status  = {'ready': False, 'reason': 'no status from run_recorder yet'}

        self.tracer   = Tracer(self, 'executor')
        self.runner   = RosSkillRunner(
            self,
            self.registry,
            VlmClient(VlmClientConfig(base_url=self.vlm_base_url, model=self.vlm_model, timeout_s=self.vlm_timeout)),
            self.image_topic,
            capture_dir=self.capture_dir
        )

        self.agent = self._make_agent()
        self.tasks = Tasks(self.registry, make_leaf_factory(self.registry, self.runner, self.agent), self.tracer,
                           self._publish_status, self.essential_devices, make_scratch=self._run_scratch,
                           on_error=self.get_logger().error)

        self._status_publisher   = self.create_publisher(String, '/orchestrator/status', STATUS_QOS)
        self._tree_subscriptions = [
            self.create_subscription(String, '/orchestrator/tree', self._on_tree, 10),
            self.create_subscription(String, '/orchestrator/tree_blind', self._on_blind_tree, 10),
            self.create_subscription(Empty, '/orchestrator/cancel', self._on_cancel, 10),
            self.create_subscription(String, RECORDER_TOPIC, self._on_recorder_status, STATUS_QOS),
        ]
        self._tick_timer         = self.create_timer(1.0 / self.tick_hz, self._tick)

        self._publish_status('idle')
        self.get_logger().info(f'{len(self.registry)} skills, {len(self.places)} places')

    def _make_agent(self) -> Agent | None:
        """The runner of agent skills, or None if agent_backend is none or its backend cannot start"""
        backend_name   = self.declare_parameter('agent_backend', 'external').value        # external | openai_compatible | scripted | none
        model          = self.declare_parameter('agent_model', 'local').value            # openai_compatible only
        base_url       = self.declare_parameter('agent_base_url', '').value              # openai_compatible only
        api_key_env    = self.declare_parameter('agent_api_key_env', 'OPENAI_API_KEY').value
        script         = self.declare_parameter('agent_script', '[]').value              # scripted: JSON, one call list per agent call
        skills_dir     = self.declare_parameter('agent_skills_dir', str(Path(get_package_share_directory('skill_registry')) / 'agent_skills')).value
        read_roots     = self.declare_parameter('agent_read_roots', ['/home/vector/eigen_vector_ws', skills_dir]).value
        python_timeout = self.declare_parameter('agent_python_timeout_s', 60.0).value
        max_turns      = self.declare_parameter('agent_max_turns', 30).value
        knowledge_dir  = self.declare_parameter('knowledge_dir', str(Path.home() / 'orch_knowledge')).value     # claims.yaml, log.jsonl
        scratch_dir    = self.declare_parameter('agent_scratch_dir', str(Path(knowledge_dir) / 'scratch')).value  # <run_id>/ in it

        if backend_name == 'none':
            return None

        try:
            backend = self._make_agent_backend(backend_name, model, base_url, api_key_env, script)
        except ValueError as error:                                 # a bad script or name: agent skills then fail
            self.get_logger().warn(f'no agent: {error}')
            return None

        config = AgentConfig(
            skills_dir=skills_dir,
            read_roots=list(read_roots),
            max_turns=max_turns,
            sandbox=SandboxConfig(scratch_dir=scratch_dir, timeout_s=python_timeout),
            knowledge=KnowledgeStore(knowledge_dir)
        )

        speaker = self.create_publisher(String, '/speak', 10)                  # unitree_interface's TTS (remapped ~/tts)
        speak   = lambda text: speaker.publish(String(data=text))

        return Agent(backend, config, RosReader(self, self.runner.latest_image, self.runner.capture_frames),
                     on_event=self._on_agent_event, speak=speak, press_evidence=self.runner.press_evidence)

    def _make_agent_backend(self, name: str, model: str, base_url: str, api_key_env: str, script: str):
        if name == 'external':
            return self._make_external_backend()

        if name == 'openai_compatible':
            return OpenAICompatibleAgentBackend(base_url, model, api_key_env)

        if name == 'scripted':
            return ScriptedAgentBackend(json.loads(script))

        raise ValueError(f'unknown agent_backend {name}')

    def _make_external_backend(self) -> ExternalAgentBackend:
        """Calls go out on the request topic; a session answers with agent_cli on the decision topic"""
        request_publisher = self.create_publisher(String, REQUEST_TOPIC, STATUS_QOS)
        reply_publisher   = self.create_publisher(String, REPLY_TOPIC, COMMAND_QOS)
        backend           = ExternalAgentBackend(
            lambda request: request_publisher.publish(String(data=json.dumps(request, default=str))),
            lambda reply: reply_publisher.publish(String(data=json.dumps(reply)))
        )

        def on_decision(message: String):
            try:
                backend.deliver(json.loads(message.data))
            except json.JSONDecodeError as error:
                reply_publisher.publish(String(data=json.dumps({'id': None, 'accepted': False, 'message': f'not JSON: {error}'})))

        self._decision_subscription = self.create_subscription(String, DECISION_TOPIC, on_decision, COMMAND_QOS)

        return backend

    def _on_agent_event(self, kind: str, text: str, level: str = 'info', **data):
        self.tracer.emit(kind, text, level=level, **data)

    # ========== Trees in ==========

    def _on_tree(self, message: String):
        self.run(message.data, blind=False)

    def _on_blind_tree(self, message: String):
        self.run(message.data, blind=True)

    def _on_recorder_status(self, message: String):
        self.recorder_status = json.loads(message.data)

    def run(self, xml_text: str, blind: bool) -> bool:
        recorder_up = self.count_publishers(RECORDER_TOPIC) > 0         # a latched ready outlives a recorder that died

        if self.require_recorder and not (recorder_up and self.recorder_status.get('ready')):
            reason = self.recorder_status.get('reason') if recorder_up else 'run_recorder is not running'
            return self.tasks.reject(f'tree refused: the recorder is not ready ({reason})')

        run_id  = uuid.uuid4().hex[:8]
        context = Context(blackboard=dict(self.places), on_event=self._on_node_event, on_record=self._on_record, run_id=run_id)

        return self.tasks.run_tree(xml_text, context, blind)

    def _run_scratch(self, run_id: str) -> str | None:
        """The run's scratch folder under the agent's scratch root; the recorder moves it into the run's bundle"""
        if self.agent is None:
            return None

        folder = Path(self.agent.config.sandbox.scratch_dir).expanduser() / run_id

        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:                                    # the run goes on; its agent calls get no notes folder
            self.get_logger().warn(f'no scratch folder for this run: {error}')
            return None

        return str(folder)

    def _on_cancel(self, message: Empty):
        self.tasks.cancel()

    def _tick(self):
        self.tasks.tick()

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

    def _on_record(self, record: dict):
        """A finished action call or an agent decision; a failure carries its feedback, for review after the run"""
        if 'decision' in record:
            text = f'{record["skill"]} decided {record["decision"]}: {record["reason"]}'
        else:
            text = f'{record["skill"]} {record["status"]} in {record["elapsed_s"]} s: {record["message"]}'

        level = 'warn' if record.get('status') == Status.FAILURE.value else 'info'
        self.tracer.emit('record', text, level=level, record=record)

    def _publish_status(self, state: str, tree: str | None = None):
        payload = {'state': state, 'tree': tree}

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

        if node.agent is not None:
            node.agent.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
