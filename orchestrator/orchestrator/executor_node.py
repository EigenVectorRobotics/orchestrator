"""Validate a behavior tree, then tick it on the NUC; every step goes to the trace"""

import json
import uuid
import threading

from pathlib import Path

import rclpy

from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import Empty, String

from ament_index_python.packages import get_package_share_directory
from skill_registry import agent_code_dir, agent_runbook_file, agent_skills_dir

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
from .code_skill import CodeRunner
from .agent_sandbox import SandboxConfig
from .tasks import CANCEL_TOPIC, STATUS_TOPIC, TASK_TOPIC, Tasks, parse_task_request
from .task_loop import CodexDecider, TaskLoop
from .planner import describe_catalog
from .recording import DEFAULT_REPO_ROOTS, repo_states
from .skill_stamps import Stamps
from .skill_writer import SkillWriter
from .contract_check import check_ros_types
from .task_agent import (COMMAND_TOPIC, OPEN_TOPIC, REPLY_TOPIC as TASK_REPLY_TOPIC, STEP_DECISION_TOPIC,
                         STEP_REPLY_TOPIC, STEP_REQUEST_TOPIC, StepGate, TaskDesk)
from .behavior_tree import Context, Status
from .ros_skill_runner import RosSkillRunner
from .press_capture import DEFAULT_CAPTURE_DIR
from .run_recorder import STATUS_TOPIC as RECORDER_TOPIC


STATUS_QOS  = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
COMMAND_QOS = QoSProfile(depth=5)


def read_text(path: Path) -> str:
    """An operator file that may go away while the node runs: no file is no text, not an error"""
    try:
        return path.read_text().strip()
    except (OSError, ValueError):            # ValueError: a hand-edited file that is not UTF-8
        return ''


def share_runbook() -> Path:
    """The runbook as the package installs it (setup.py); the agent folder's copy wins over it"""
    return Path(get_package_share_directory('orchestrator')) / 'agent' / 'runbook.md'


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
        mode                  = self.declare_parameter('mode', 'task').value   # task: a session does the task step by step; tree: planner_node writes a tree
        self.recorder_status  = {'ready': False, 'reason': 'no status from run_recorder yet'}

        self.tracer   = Tracer(self, 'executor')
        self._speaker = self.create_publisher(String, '/speak', 10)            # unitree_interface's TTS (remapped ~/tts)
        self.runner   = RosSkillRunner(
            self,
            self.registry,
            VlmClient(VlmClientConfig(base_url=self.vlm_base_url, model=self.vlm_model, timeout_s=self.vlm_timeout)),
            self.image_topic,
            capture_dir=self.capture_dir
        )

        self.agent = self._make_agent()
        self.gate  = self._make_gate() if self.declare_parameter('step_gate', True).value else None
        self.code  = self._make_code()
        build_leaf = make_leaf_factory(self.registry, self.runner, self.agent, self.code)
        self.code.build_leaf = build_leaf              # a call inside a code skill is a leaf of its own
        self.tasks = Tasks(self.registry, build_leaf, self.tracer,
                           self._publish_status, self.essential_devices, make_scratch=self._run_scratch,
                           on_error=self.get_logger().error)

        self._status_publisher   = self.create_publisher(String, STATUS_TOPIC, STATUS_QOS)
        self._tree_subscriptions = [
            self.create_subscription(String, '/orchestrator/tree', self._on_tree, 10),
            self.create_subscription(String, '/orchestrator/tree_blind', self._on_blind_tree, 10),
            self.create_subscription(Empty, CANCEL_TOPIC, self._on_cancel, 10),
            self.create_subscription(String, RECORDER_TOPIC, self._on_recorder_status, STATUS_QOS),
        ]
        # contracts that change on disk load while it runs, checked as write_skill checks them (skill_writer.py)
        contracts_dir            = Path(self.get_parameter('contracts_directory').value)
        self.stamps              = self._make_stamps(contracts_dir)
        self.writer              = SkillWriter(self.registry, contracts_dir, check_ros_types, self.runner.prepare,
                                               code_dir=self.code.code_dir, stamps=self.stamps)
        self.desk                = self._make_desk() if mode == 'task' else None
        self.loop                = self._make_loop() if mode == 'task' else None
        self._tick_timer         = self.create_timer(1.0 / self.tick_hz, self._tick)
        self._refresh_timer      = self.create_timer(5.0, self.writer.refresh)

        self._publish_status('idle')
        self.get_logger().info(f'{len(self.registry)} skills, {len(self.places())} places')

    def _make_stamps(self, contracts_dir: Path) -> Stamps:
        """What a skill was proved against. repo_roots is also run_recorder's parameter and both nodes default to
        DEFAULT_REPO_ROOTS, but they are declared per node: set it on one only and the stamps and the run bundle
        answer 'which code is this' about different repos, so the roots are logged at start"""
        roots  = self.declare_parameter('repo_roots', DEFAULT_REPO_ROOTS).value
        stamps = Stamps(contracts_dir, list(roots), code_dir=self.code.code_dir)
        found  = len(repo_states(stamps.repo_roots))

        if found:
            self.get_logger().info(f'skill stamps: {found} repos under {", ".join(stamps.repo_roots)}')
        else:
            self.get_logger().warning(f'skill stamps: NO repo under {", ".join(stamps.repo_roots)}; '
                                      f'a skill written here can never be proved. Check repo_roots')

        return stamps

    def _stale(self) -> dict[str, str]:
        return self.stamps.reasons(self.registry)

    def _make_desk(self) -> TaskDesk:
        """Task mode: a session (agent_cli) does each task step by step through these topics"""
        open_publisher  = self.create_publisher(String, OPEN_TOPIC, STATUS_QOS)
        reply_publisher = self.create_publisher(String, TASK_REPLY_TOPIC, COMMAND_QOS)
        publish_reply   = lambda message: reply_publisher.publish(String(data=json.dumps(message, default=str)))
        knowledge       = self.agent.config.knowledge if self.agent else None
        self._runbook_given = self.declare_parameter('runbook', '').value
        self._say_which_runbook()
        writes          = self.declare_parameter('write_skill', True).value

        if writes:
            self.get_logger().info(f'write_skill on: the agent saves contracts in {self.writer.directory}')
        else:
            self.get_logger().warning('write_skill off: the agent cannot save a contract; a file put in by hand still loads')
        desk            = TaskDesk(
            self.tasks,
            lambda message: open_publisher.publish(String(data=json.dumps(message, default=str))),
            publish_reply,
            self._speak,
            known=lambda: knowledge.prompt_section(observations=True) if knowledge else '',
            runbook=lambda: read_text(self._runbook_file()),
            max_steps=self.declare_parameter('task_max_steps', 40).value,
            max_task_s=self.declare_parameter('task_max_s', 900.0).value,
            idle_s=self.declare_parameter('task_idle_s', 120.0).value,
            gate=self.gate,
            writer=self.writer if writes else None,
            stale=self._stale,
            ran=self.stamps.ran,
        )

        def on_command(message: String):                # a run blocks until its step ends: not on the spin thread
            try:
                command = json.loads(message.data)
            except json.JSONDecodeError as error:
                return publish_reply({'id': None, 'ok': False, 'message': f'not JSON: {error}'})

            threading.Thread(target=desk.command, args=(command,), daemon=True).start()

        self._task_subscriptions = [
            self.create_subscription(String, TASK_TOPIC, self._on_task, 10),
            self.create_subscription(String, COMMAND_TOPIC, on_command, COMMAND_QOS),
        ]

        return desk

    def _make_loop(self) -> TaskLoop | None:
        """task_agent codex: the executor runs the loop, Codex decides each step (no session outside)"""
        kind = self.declare_parameter('task_agent', 'session').value     # session (agent_cli) | codex

        if kind == 'session':
            return None

        if kind != 'codex':
            raise ValueError(f'unknown task_agent {kind}: use session or codex')

        decider = CodexDecider(model=self.declare_parameter('codex_model', 'gpt-6-astra').value,
                               effort=self.declare_parameter('codex_effort', 'high').value,
                               timeout_s=self.declare_parameter('codex_timeout_s', 180.0).value)
        self.get_logger().info(f'task agent: codex {decider.model}, one decision per turn')

        return TaskLoop(self.desk, decider, lambda: describe_catalog(self.registry, self.essential_devices, self._stale()),
                        self.runner.latest_image, self.tracer)

    def _runbook_file(self) -> Path:
        """What the robot is: the operator's file wins, else the one the package ships, so a fresh robot is never
        without a runbook. Chosen again at each task open, so a file written or removed while it runs is seen"""
        wanted = Path(self._runbook_given).expanduser() if self._runbook_given else agent_runbook_file()

        return wanted if wanted.is_file() else share_runbook()

    def _say_which_runbook(self) -> None:
        """Once at start: which runbook the agent will get, and whether the operator's own file was found"""
        wanted = Path(self._runbook_given).expanduser() if self._runbook_given else agent_runbook_file()
        found  = self._runbook_file()

        if not wanted.is_file():
            self.get_logger().warning(f'no runbook at {wanted}: the one the package installs is used')

        if found.is_file():
            self.get_logger().info(f'runbook: {found}')
        else:
            self.get_logger().warning(f'no runbook at {found} either: the agent gets no description of the robot')

    def _make_code(self) -> CodeRunner:
        """Code skills: Python in the sandbox that calls other skills back through this executor"""
        code_dir = self.declare_parameter('code_dir', str(agent_code_dir())).value
        scratch  = self.declare_parameter('code_scratch_dir', str(Path.home() / 'orch_knowledge' / 'scratch' / 'code')).value
        timeout  = self.declare_parameter('code_cpu_s', 60.0).value

        return CodeRunner(self.registry, SandboxConfig(scratch_dir=scratch, timeout_s=timeout),
                          code_dir=code_dir, approve=self._approve_inside_code)

    def _approve_inside_code(self, contract, inputs: dict, reasons: list[str]) -> tuple[bool, str]:
        """A code skill is approved once; a skill the operator's gate.yaml names asks again, inside the code"""
        if self.desk is None or self.gate is None:
            return True, ''

        decision = self.desk.ask_step({'skill': contract.name, 'inputs': inputs, 'in_code': True,
                                       'subgoal': '; '.join(reasons)}, step='inside the code')

        return decision['approve'], decision['reason']

    def _make_gate(self) -> StepGate:
        """The real robot, at first: a person approves each step (agent_cli step approve|refuse, at a terminal)"""
        request_publisher = self.create_publisher(String, STEP_REQUEST_TOPIC, STATUS_QOS)
        reply_publisher   = self.create_publisher(String, STEP_REPLY_TOPIC, COMMAND_QOS)
        gate              = StepGate(lambda request: request_publisher.publish(String(data=json.dumps(request, default=str))))

        def on_decision(message: String):
            try:
                reply = gate.decide(json.loads(message.data))
            except (json.JSONDecodeError, AttributeError) as error:
                reply = {'id': None, 'accepted': False, 'message': f'bad decision: {error}'}

            reply_publisher.publish(String(data=json.dumps(reply)))

        self._gate_subscription = self.create_subscription(String, STEP_DECISION_TOPIC, on_decision, COMMAND_QOS)
        self.get_logger().info('step gate on: each step waits for a person (agent_cli step approve, or Mission Control)')

        return gate

    def _make_agent(self) -> Agent | None:
        """The runner of agent skills, or None if agent_backend is none or its backend cannot start"""
        backend_name   = self.declare_parameter('agent_backend', 'external').value        # external | openai_compatible | scripted | none
        model          = self.declare_parameter('agent_model', 'local').value            # openai_compatible only
        base_url       = self.declare_parameter('agent_base_url', '').value              # openai_compatible only
        api_key_env    = self.declare_parameter('agent_api_key_env', 'OPENAI_API_KEY').value
        script         = self.declare_parameter('agent_script', '[]').value              # scripted: JSON, one call list per agent call
        skills_dir     = self.declare_parameter('agent_skills_dir', str(agent_skills_dir())).value
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

        return Agent(backend, config, RosReader(self, self.runner.latest_image, self.runner.capture_frames, self.runner.frames.at),
                     on_event=self._on_agent_event, speak=self._speak, press_evidence=self.runner.press_evidence)

    def _speak(self, text: str):
        self._speaker.publish(String(data=text))

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
        if not self._recorder_ready('tree'):
            return False

        return self.tasks.run_tree(xml_text, self._new_context(), blind)

    def _on_task(self, message: String):
        """Task mode: a task request opens a task for the session"""
        task, blind = parse_task_request(message.data)

        if self._recorder_ready('task'):
            opened = self.desk.open(task, self._new_context(), blind)

            if self.loop is not None:
                self.loop.start(opened)

    def _recorder_ready(self, what: str) -> bool:
        recorder_up = self.count_publishers(RECORDER_TOPIC) > 0         # a latched ready outlives a recorder that died

        if self.require_recorder and not (recorder_up and self.recorder_status.get('ready')):
            reason = self.recorder_status.get('reason') if recorder_up else 'run_recorder is not running'
            return self.tasks.reject(f'{what} refused: the recorder is not ready ({reason})')

        return True

    def _new_context(self) -> Context:
        return Context(blackboard=dict(self.places()), on_event=self._on_node_event, on_record=self._on_record,
                       run_id=uuid.uuid4().hex[:8])

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

        if self.desk:
            self.desk.check()

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
        node.tasks.cancel()                             # halt a running step

        node.runner.shutdown()

        if node.agent is not None:
            node.agent.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
