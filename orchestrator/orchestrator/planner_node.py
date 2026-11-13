"""Plan a tree for a task with an LLM, then send it to the executor"""

import json
import time
import threading

import rclpy

from pathlib import Path

from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import Int32, String
from ament_index_python.packages import get_package_share_directory
from skill_registry import load_tree

from .trace import TRACE_QOS, TRACE_TOPIC, Tracer
from .planner import Planner, load_tree_library, speech_left_s
from .node_setup import load_place_floors, load_skills
from .llm_backends import ClaudeBackend, OpenAICompatibleBackend, ScriptedBackend
from .planner_external import ANSWER_TOPIC, REQUEST_TOPIC, ExternalPlannerBackend


LIVE_QOS = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
ANSWER_QOS = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
FLOOR_QOS       = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
SPEAK_TOPIC     = '/speak'                      # the say skill's topic: unitree_interface speaks it
NO_LIBRARY_TEXT = 'No existing behavior tree. Generating a new behavior tree.'
GENERATED_TEXT  = 'No existing behavior tree fits. I generated a new one.'


class PlannerNode(Node):
    def __init__(self):
        super().__init__('orchestrator_planner')

        self.registry, self.places, self.essential_devices = load_skills(self)
        self.place_floors = load_place_floors(self.get_parameter('places_file').value)   # key -> floor, for the task message
        self.robot_floor  = None                                                          # the latest floor on floor_topic

        registry_share = Path(get_package_share_directory('skill_registry'))

        self.backend_name        = self.declare_parameter('backend', 'claude').value     # claude | openai_compatible | scripted | external
        self.external_timeout_s  = self.declare_parameter('external_timeout_s', 600.0).value   # external: wait for the session
        self.model               = self.declare_parameter('model', 'claude-opus-5').value
        self.effort              = self.declare_parameter('effort', 'high').value
        self.base_url            = self.declare_parameter('base_url', '').value          # openai_compatible only
        self.api_key_env         = self.declare_parameter('api_key_env', 'OPENAI_API_KEY').value
        self.scripted_tree       = self.declare_parameter('scripted_tree', str(Path(get_package_share_directory('orchestrator')) / 'test_trees' / 'call_elevator.xml')).value
        self.max_attempts        = self.declare_parameter('max_attempts', 3).value
        self.use_live_skills     = self.declare_parameter('use_live_skills', True).value
        self.auto_run            = self.declare_parameter('auto_run', True).value
        self.tree_library        = self.declare_parameter('tree_library', str(registry_share / 'trees')).value   # the tree library; '' = none
        self.preflight_tree      = self.declare_parameter('preflight_tree', str(registry_share / 'preflight' / 'preflight.xml')).value   # '' = none
        self.preflight_timeout_s = self.declare_parameter('preflight_timeout_s', 600.0).value
        self.floor_topic         = self.declare_parameter('floor_topic', '/slam_elevatorlio/floor').value   # Int32, latched; '' = none

        self.tracer      = Tracer(self, 'planner')
        self.library     = load_tree_library(self.tree_library) if self.tree_library else {}
        self.preflight   = load_tree(self.preflight_tree) if self.preflight_tree else ''
        self._preflight_done   = threading.Event()
        self._preflight_status = ''
        self.live_skills = None
        self.busy        = threading.Lock()
        self.planner     = Planner(
            self._make_backend(),
            self.registry,
            self.essential_devices,
            self.max_attempts,
            on_event=self.tracer.emit,
            library=self.library
        )

        self._tree_publisher  = self.create_publisher(String, '/orchestrator/tree', 10)
        self._blind_publisher = self.create_publisher(String, '/orchestrator/tree_blind', 10)
        self._plan_publisher  = self.create_publisher(String, '/orchestrator/plan', LIVE_QOS)
        self._speak_publisher = self.create_publisher(String, SPEAK_TOPIC, 10)
        self._last_speech     = ('', 0.0)       # the last sentence on /speak and when it started (time.time())
        self._planner_subscriptions = [
            self.create_subscription(String, '/orchestrator/task', self._on_task, 10),
            self.create_subscription(String, '/skills/live', self._on_live, LIVE_QOS),
            self.create_subscription(String, TRACE_TOPIC, self._on_trace, TRACE_QOS),
            self.create_subscription(String, SPEAK_TOPIC, self._on_speak, 10),
        ]

        if self.floor_topic:
            self._planner_subscriptions.append(self.create_subscription(Int32, self.floor_topic, self._on_floor, FLOOR_QOS))

        self.get_logger().info(f'planner up: {self.planner.backend.name} {self.model}, {len(self.registry)} skills, '
                               f'{len(self.library)} existing trees')

    def _make_backend(self):
        if self.backend_name == 'claude':
            return ClaudeBackend(self.model, self.effort)

        if self.backend_name == 'openai_compatible':
            return OpenAICompatibleBackend(self.base_url, self.model, self.api_key_env)

        if self.backend_name == 'external':
            return self._make_external_backend()

        if self.backend_name == 'scripted':
            tree = load_tree(self.scripted_tree)
            return ScriptedBackend([f'Scripted plan from {Path(self.scripted_tree).name}.\n<tree>\n{tree}\n</tree>'])

        raise ValueError(f'unknown backend {self.backend_name}')

    def _make_external_backend(self) -> ExternalPlannerBackend:
        """Calls go out on the request topic; a session answers with planner_cli on the answer topic"""
        request_publisher = self.create_publisher(String, REQUEST_TOPIC, LIVE_QOS)
        backend           = ExternalPlannerBackend(
            lambda request: request_publisher.publish(String(data=json.dumps(request, default=str))),
            self.external_timeout_s
        )
        self._answer_subscription = self.create_subscription(
            String, ANSWER_TOPIC, lambda message: backend.deliver(json.loads(message.data)), ANSWER_QOS)

        return backend

    def _on_floor(self, message: Int32):
        self.robot_floor = str(message.data)

    def _on_live(self, message: String):
        self.live_skills = set(json.loads(message.data))

    def _on_task(self, message: String):
        """A plain sentence, or JSON: {"task": "...", "blind": true}"""
        try:
            request = json.loads(message.data)
            task    = str(request['task'])
        except (json.JSONDecodeError, TypeError, KeyError):
            request, task = {}, message.data

        if not self.busy.acquire(blocking=False):
            self.tracer.emit('error', 'still planning the last task; this one is dropped', level='warn')
            return

        threading.Thread(target=self._plan, args=(task, bool(request.get('blind', False))), daemon=True).start()

    def _on_speak(self, message: String):
        """Any sentence on /speak: the TTS has no queue, so the next one must wait for it"""
        self._last_speech = (message.data, time.time())

    def _speak(self, text: str):
        """Say text once the last sentence on /speak is done; a new sentence cuts the one being spoken"""
        time.sleep(speech_left_s(*self._last_speech, time.time()))
        self._speak_publisher.publish(String(data=text))

    def _on_trace(self, message: String):
        """The executor's end of the preflight run"""
        event = json.loads(message.data)

        if event.get('source') == 'executor' and event.get('tree_id') == 'preflight' and event.get('kind') in ('finished', 'halted', 'error'):
            self._preflight_status = event.get('status') or event.get('kind')
            self._preflight_done.set()

    def _run_preflight(self) -> bool:
        """Send the preflight tree to the executor and wait for its end: stacks up, G1 in ready locomotion"""
        self._preflight_done.clear()
        self._preflight_status = ''
        self.tracer.emit('preflight', 'preflight sent to the executor')
        self._tree_publisher.publish(String(data=self.preflight))

        if not self._preflight_done.wait(self.preflight_timeout_s):
            self.tracer.emit('error', f'preflight did not end within {self.preflight_timeout_s:g} s; not planning', level='error')
            return False

        if self._preflight_status != 'success':
            self.tracer.emit('error', f'preflight ended with {self._preflight_status}; not planning', level='error')
            return False

        return True

    def _plan(self, task: str, blind: bool):
        try:
            if self.preflight and not self._run_preflight():
                return

            # Reuse or a new tree is known only after the plan, unless there is nothing to reuse
            if not self.library:
                self._speak(NO_LIBRARY_TEXT)

            live   = self.live_skills if self.use_live_skills else None
            result = self.planner.plan(task, self.places, live, blind, self.place_floors, self.robot_floor)

            if not result.ok:
                return

            if result.reused:
                self._speak(f'Using the existing behavior tree {result.reused.replace("_", " ")}.')
            elif self.library:
                self._speak(GENERATED_TEXT)

            self._plan_publisher.publish(String(data=result.tree))

            if self.auto_run:
                publisher = self._blind_publisher if blind else self._tree_publisher
                publisher.publish(String(data=result.tree))
                origin = f'existing tree {result.reused}' if result.reused else 'tree'
                self.tracer.emit('sent', f'{origin} sent to the executor' + (' (blind)' if blind else ''))
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
