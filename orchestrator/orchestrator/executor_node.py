"""Task mode on the NUC: open a task, run each step the agent asks for, and trace every step"""

import json
import uuid
import threading

from pathlib import Path

import rclpy

from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import Empty, String

from ament_index_python.packages import get_package_share_directory
from skill_registry import agent_knowledge_dir, agent_runbook_file, stamps_file

from .trace import Tracer
from .knowledge import KnowledgeStore
from .step_grades import StepGrades
from .graduation import Graduation
from .node_setup import load_skills
from .vlm_client import VlmClient, VlmClientConfig
from .skill_leaf import SkillLeaf, make_leaf_factory
from .code_skill import CodeRunner
from .tree_skill import TreeRunner
from .sandbox import SandboxConfig
from .tasks import Tasks, parse_task_request
from skill_registry.provenance import DEFAULT_REPO_ROOTS
from skill_registry.skill_stamps import SkillClock, Stamps
from skill_registry.skill_writer import SkillWriter
from skill_registry.skill_review import composition, grade_version
from skill_registry.contract_check import check_ros_types
from .step_gate import StepGate
from .task_desk import TaskDesk
from .behavior_tree import Context, Status
from .ros_skill_runner import RosSkillRunner
from .sensing import CameraStream
from .qos import COMMAND_QOS, LATCHED_QOS
from .topics import (CANCEL_TOPIC, COMMAND_TOPIC, NOTE_TOPIC, OPEN_TOPIC, RECORDER_TOPIC, REPLY_TOPIC, STATUS_TOPIC, STEP_DECISION_TOPIC,
                     STEP_REPLY_TOPIC, STEP_REQUEST_TOPIC, STOP_STEP_TOPIC, TASK_TOPIC)


def read_text(path: Path) -> str:
    """An operator file that may go away while the node runs: no file is no text, not an error"""
    try:
        return path.read_text().strip()
    except (OSError, ValueError):            # ValueError: a hand-edited file that is not UTF-8
        return ''


def share_runbook() -> Path:
    """The runbook as the package installs it: the same file from git, for when the workspace link is missing"""
    return Path(get_package_share_directory('orchestrator')) / 'agent' / 'runbook.md'


class ExecutorNode(Node):
    def __init__(self):
        super().__init__('orchestrator_executor')

        self.registry, self.places, self.essential_devices = load_skills(self)
        self.skills_dir   = Path(self.get_parameter('skills_dir').value)     # the library, declared by load_skills

        self.tick_hz      = self.declare_parameter('tick_hz', 10.0).value
        self.image_topic  = self.declare_parameter('image_topic', '/camera/camera/color/image_raw').value
        self.vlm_base_url = self.declare_parameter('vlm_base_url', 'http://vector-thor:8090/v1').value
        self.vlm_model    = self.declare_parameter('vlm_model', 'local').value
        self.vlm_timeout  = self.declare_parameter('vlm_timeout_s', 40.0).value    # Cosmos on the Thor: one request at a time
        self.require_recorder = self.declare_parameter('require_recorder', False).value   # true: no task runs unrecorded
        self.scratch_dir      = self.declare_parameter('agent_scratch_dir', str(Path.home() / 'orch_knowledge' / 'scratch')).value  # <run_id>/ in it
        self.recorder_status  = {'ready': False, 'reason': 'no status from run_recorder yet'}

        self.tracer   = Tracer(self, 'executor')
        self._speaker = self.create_publisher(String, '/speak', 10)            # unitree_interface's TTS (remapped ~/tts)
        self.camera   = CameraStream(self, self.image_topic)
        self.runner   = RosSkillRunner(
            self,
            self.registry,
            self.camera,
            VlmClient(VlmClientConfig(base_url=self.vlm_base_url, model=self.vlm_model, timeout_s=self.vlm_timeout)),
        )

        knowledge_dir  = self.declare_parameter('knowledge_dir', str(agent_knowledge_dir())).value     # claims.yaml, log.jsonl
        self.knowledge = KnowledgeStore(knowledge_dir)        # the task desk's; its code clock comes with the stamps
        self.gate  = self._make_gate() if self.declare_parameter('step_gate', True).value else None
        self.code  = self._make_code()
        self.trees = TreeRunner(self.registry, str(self.skills_dir))
        build_leaf = make_leaf_factory(self.registry, self.runner, self.code, self.trees)
        self.code.build_leaf = self.trees.build_leaf = build_leaf   # a call inside a code skill or a tree is a leaf too
        self.tasks = Tasks(self.registry, build_leaf, self.tracer,
                           self._publish_status, self.essential_devices, make_scratch=self._run_scratch,
                           on_error=self.get_logger().error)

        self._status_publisher   = self.create_publisher(String, STATUS_TOPIC, LATCHED_QOS)
        self.create_subscription(Empty, CANCEL_TOPIC, self._on_cancel, 10)
        self.create_subscription(String, STOP_STEP_TOPIC, self._on_stop_step, 10)
        self.create_subscription(String, NOTE_TOPIC, self._on_note, 10)
        self.create_subscription(String, RECORDER_TOPIC, self._on_recorder_status, LATCHED_QOS)
        # skills that change on disk load while it runs, checked as write_skill checks them (skill_writer.py)
        self.stamps              = self._make_stamps(self.skills_dir)
        self.knowledge.code      = SkillClock(self.stamps, self.registry)     # a claim about a skill counts evidence on its code now
        self.knowledge.code.save(self.knowledge.folder)                       # and the knowledge CLI uses the same clock
        self.writer              = SkillWriter(self.registry, self.skills_dir, check_ros_types, self.runner.prepare,
                                               stamps=self.stamps)
        self.desk                = self._make_desk()
        self._tick_timer         = self.create_timer(1.0 / self.tick_hz, self._tick)
        self._refresh_timer      = self.create_timer(5.0, self.writer.refresh)

        self._publish_status('idle')
        self.get_logger().info(f'{len(self.registry)} skills, {len(self.places())} places')

    def _make_stamps(self, skills_dir: Path) -> Stamps:
        """What a skill was proved against. repo_roots is also run_recorder's parameter and both nodes default to
        DEFAULT_REPO_ROOTS, but they are declared per node: set it on one only and the stamps and the run bundle
        answer 'which code is this' about different repos, so the roots are logged at start"""
        roots  = self.declare_parameter('repo_roots', DEFAULT_REPO_ROOTS).value
        path   = self.declare_parameter('stamps_file', str(stamps_file())).value
        stamps = Stamps(skills_dir, list(roots), path=path)
        found  = len(stamps.repo_states())

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
        open_publisher  = self.create_publisher(String, OPEN_TOPIC, LATCHED_QOS)
        reply_publisher = self.create_publisher(String, REPLY_TOPIC, COMMAND_QOS)
        publish_reply   = lambda message: reply_publisher.publish(String(data=json.dumps(message, default=str)))
        self._runbook   = Path(self.declare_parameter('runbook', str(agent_runbook_file())).value).expanduser()
        self._say_which_runbook()
        writes          = self.declare_parameter('write_skill', True).value

        if writes:
            self.get_logger().info(f'write_skill on: the agent saves contracts in {self.writer.directory}')
        else:
            self.get_logger().warning('write_skill off: the agent cannot save a contract; a file put in by hand still loads')
        steps           = StepGrades(self.knowledge.folder, version=lambda name: grade_version(self.skills_dir, name),
                                     robot=self.declare_parameter('on_robot', False).value)   # the launch sets it; off: no grade graduates
        desk            = TaskDesk(
            self.tasks,
            lambda message: open_publisher.publish(String(data=json.dumps(message, default=str))),
            publish_reply,
            self._speak,
            knowledge=self.knowledge,
            runbook=lambda: read_text(self._runbook_file()),
            max_steps=self.declare_parameter('task_max_steps', 40).value,
            max_task_s=float(self.declare_parameter('task_max_s', 1800.0, ParameterDescriptor(dynamic_typing=True)).value),   # 0 is an int
            idle_s=self.declare_parameter('task_idle_s', 120.0).value,
            gate=self.gate,
            writer=self.writer if writes else None,
            stale=self._stale,
            ran=self.stamps.ran,
            composition=lambda name: composition(self.skills_dir, name),
            steps=steps,
            graduation=Graduation(steps, composition=lambda name: composition(self.skills_dir, name),
                                  written_of=lambda name: getattr(self.registry.get(name), 'maturity', None)),
            trust_tested=self.declare_parameter('gate_trusts_tested', True).value,
        )

        def on_command(message: String):                # a run blocks until its step ends: not on the spin thread
            try:
                command = json.loads(message.data)
            except json.JSONDecodeError as error:
                return publish_reply({'id': None, 'ok': False, 'message': f'not JSON: {error}'})

            threading.Thread(target=desk.command, args=(command,), daemon=True).start()

        self.create_subscription(String, TASK_TOPIC, self._on_task, 10)
        self.create_subscription(String, COMMAND_TOPIC, on_command, COMMAND_QOS)

        return desk

    def _runbook_file(self) -> Path:
        """The runbook is in git (orchestrator/agent/runbook.md), and the agent folder links to it
        (scripts/link_agent_dir.sh). Without the link, the copy that the package installs: the agent never runs
        without the robot's description and safety rules. Chosen at each task open, so a fixed link is seen"""
        return self._runbook if self._runbook.is_file() else share_runbook()

    def _say_which_runbook(self) -> None:
        """Once at start"""
        if self._runbook.is_file():
            self.get_logger().info(f'runbook: {self._runbook}')
        else:
            self.get_logger().warning(f'no runbook at {self._runbook} (run scripts/link_agent_dir.sh): the agent gets '
                                      f'the copy the package installs, {share_runbook()}')

    def _make_code(self) -> CodeRunner:
        """Code skills: Python in the sandbox that calls other skills back through this executor"""
        scratch  = self.declare_parameter('code_scratch_dir', str(Path.home() / 'orch_knowledge' / 'scratch' / 'code')).value
        timeout  = self.declare_parameter('code_cpu_s', 60.0).value

        return CodeRunner(self.registry, SandboxConfig(scratch_dir=scratch, timeout_s=timeout),
                          skills_dir=self.skills_dir)

    def _make_gate(self) -> StepGate:
        """The real robot, at first: a person approves each step (agent_cli step approve|refuse, at a terminal)"""
        request_publisher = self.create_publisher(String, STEP_REQUEST_TOPIC, LATCHED_QOS)
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

    def _speak(self, text: str):
        self._speaker.publish(String(data=text))

    # ========== Tasks in ==========

    def _on_recorder_status(self, message: String):
        self.recorder_status = json.loads(message.data)

    def _on_task(self, message: String):
        """Task mode: a task request opens a task for the session"""
        task, options = parse_task_request(message.data)

        if self._recorder_ready():
            self.desk.open(task, self._new_context(), **options)

    def _recorder_ready(self) -> bool:
        recorder_up = self.count_publishers(RECORDER_TOPIC) > 0         # a latched ready outlives a recorder that died

        if self.require_recorder and not (recorder_up and self.recorder_status.get('ready')):
            reason = self.recorder_status.get('reason') if recorder_up else 'run_recorder is not running'
            return self.tasks.reject(f'task refused: the recorder is not ready ({reason})')

        return True

    def _new_context(self) -> Context:
        return Context(blackboard=dict(self.places()), on_event=self._on_node_event, on_record=self._on_record,
                       run_id=uuid.uuid4().hex[:8])

    def _run_scratch(self, run_id: str) -> str | None:
        """The run's scratch folder under the agent's scratch root; the recorder moves it into the run's bundle"""
        folder = Path(self.scratch_dir).expanduser() / run_id

        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:                                    # the run goes on, with no notes folder
            self.get_logger().warn(f'no scratch folder for this run: {error}')
            return None

        return str(folder)

    def _on_cancel(self, message: Empty):
        self.tasks.cancel()

    def _on_note(self, message: String):
        try:
            note = json.loads(message.data)
            self.desk.note(str(note['run_id']), str(note['text']))
        except (ValueError, TypeError, KeyError) as error:
            self.get_logger().warn(f'a note that cannot be read: {error!r}')

    def _on_stop_step(self, message: String):
        try:
            stop = json.loads(message.data)
            self.tasks.stop_step(str(stop['run_id']), int(stop['step']), str(stop.get('reason') or ''))
        except (ValueError, TypeError, KeyError) as error:
            self.get_logger().warn(f'a stop that cannot be read: {error!r}')

    def _tick(self):
        self.tasks.tick()
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
        """A finished action call; a failure carries its feedback, for review after the run"""
        text  = f'{record["skill"]} {record["status"]} in {record["elapsed_s"]} s: {record["message"]}'

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
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
