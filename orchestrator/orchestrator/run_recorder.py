"""Record every orchestrator run on the NUC (see recording.py); refuse runs when the disk is too full.

Subscribes to the trace and the task topic; records a bag of the chosen topics for each task run; publishes
/orchestrator/recorder/status (latched JSON: ready, reason, free_gb, session). The executor refuses a task while
the status is not ready, when its require_recorder is true.
"""

import json
import time
import signal
import subprocess

from pathlib import Path

import rclpy

from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import String

from skill_registry import agent_code_dir

from .tasks import TASK_TOPIC
from .trace import TRACE_QOS, TRACE_TOPIC
from .node_setup import declare_sources
from .task_agent import COMMAND_TOPIC, OPEN_TOPIC, REPLY_TOPIC, STEP_DECISION_TOPIC, STEP_REPLY_TOPIC, STEP_REQUEST_TOPIC
from .ros_skill_runner import LATCHED_QOS
from .recording import (DEFAULT_REPO_ROOTS, Recorder, RecorderConfig, directory_digests, file_digest, find_repos,
                        git_state, largest_runs)


STATUS_TOPIC = '/orchestrator/recorder/status'
BAG_STOP_S   = 10.0                             # SIGINT, then this long to close the file, then SIGKILL

BAG_TOPICS = [
    TRACE_TOPIC, OPEN_TOPIC, COMMAND_TOPIC, REPLY_TOPIC, STEP_REQUEST_TOPIC, STEP_DECISION_TOPIC, STEP_REPLY_TOPIC,
    '/speak', '/route_goal', '/route_planner/status', '/nav_state', '/state_estimation', '/odom_base_link',
    '/right_ee_pose', '/left_ee_pose', '/track_3d/is_tracking', '/track_3d/selected_target_3d', '/slam_elevatorlio/status',
]


class RosBag:
    """`ros2 bag record` in a subprocess. stop() sends SIGINT and returns; reap() (on a timer) waits for the
    process to close its file, and kills it after BAG_STOP_S"""

    def __init__(self, node: Node, topics: list[str]):
        self.node     = node
        self.topics   = topics
        self.process  = None
        self.stopping = []                      # (process, SIGINT time)

    def start(self, folder: Path):
        self.stop()
        self.process = subprocess.Popen(['ros2', 'bag', 'record', '-s', 'mcap', '-o', str(folder), *self.topics],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.process is not None:
            self.process.send_signal(signal.SIGINT)
            self.stopping.append((self.process, time.monotonic()))
            self.process = None

    def reap(self, wait: bool = False):
        for process, since in list(self.stopping):
            if wait:
                try:
                    process.wait(timeout=max(0.0, BAG_STOP_S - (time.monotonic() - since)))
                except subprocess.TimeoutExpired:
                    pass

            if process.poll() is not None:
                self.stopping.remove((process, since))
            elif time.monotonic() - since > BAG_STOP_S:
                self.node.get_logger().error(f'ros2 bag did not stop in {BAG_STOP_S:.0f} s; killed, its last file may be incomplete')
                process.kill()
                self.stopping.remove((process, since))


class RunRecorderNode(Node):
    def __init__(self):
        super().__init__('orchestrator_recorder')

        root        = self.declare_parameter('root', RecorderConfig.root).value
        min_free_gb = self.declare_parameter('min_free_gb', RecorderConfig.min_free_gb).value
        repo_roots  = self.declare_parameter('repo_roots', DEFAULT_REPO_ROOTS).value
        record_bag  = self.declare_parameter('bag', True).value
        bag_topics  = self.declare_parameter('bag_topics', BAG_TOPICS).value
        code_dir    = self.declare_parameter('code_dir', str(agent_code_dir())).value

        contracts, places_file, map_info = declare_sources(self)

        provenance = {
            'git':    [git_state(repo) for repo in find_repos(repo_roots)],
            'places': {'path': places_file, 'sha256': file_digest(Path(places_file))} if places_file else {'map_info': map_info},
        }
        # read at the start of each run, not once: the agent writes contracts while the session runs (write_skill)
        library = lambda: {'contracts': directory_digests(Path(contracts), '*.yaml'),
                           'code':      directory_digests(Path(code_dir), '*.py')}

        self.bag      = RosBag(self, list(bag_topics)) if record_bag else None
        self.recorder = Recorder(RecorderConfig(root=root, min_free_gb=min_free_gb), provenance,
                                 self.bag, library=library)

        self._status_publisher = self.create_publisher(String, STATUS_TOPIC, LATCHED_QOS)
        self._subscriptions    = [       # not self._subscriptions of Node: that name is taken by rclpy
            self.create_subscription(String, TRACE_TOPIC, self._json(self.recorder.on_event), TRACE_QOS),
            self.create_subscription(String, TASK_TOPIC, lambda message: self.recorder.on_task(message.data), 10),
        ]
        self._was_ready   = None
        self._space_timer = self.create_timer(5.0, self._check_space)
        self._reap_timer  = self.create_timer(0.5, self.bag.reap) if self.bag else None

        self._check_space()
        self.get_logger().info(f'recording to {self.recorder.path}')

    def _json(self, handle):
        """A subscription callback that parses the message's JSON and hands it on"""
        def callback(message: String):
            try:
                value = json.loads(message.data)
            except json.JSONDecodeError as error:
                self.get_logger().warn(f'not JSON, not recorded: {error}')
                return

            handle(value)

        return callback

    def _check_space(self):
        self.recorder.check_bag_space()

        ready, reason = self.recorder.ready()

        if ready == self._was_ready:                # latched: publish when it changes, not every 5 s
            return

        self._was_ready = ready
        status          = {'ready': ready, 'reason': reason, 'free_gb': round(self.recorder.free_gb(), 1), 'session': str(self.recorder.path)}

        if not ready:
            status['largest_runs'] = largest_runs(self.recorder.path.parent, 5)

        self._status_publisher.publish(String(data=json.dumps(status)))
        (self.get_logger().info if ready else self.get_logger().error)(f'recorder {"ready" if ready else "not ready"}: {reason}')


def main():
    rclpy.init()
    node = RunRecorderNode()

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.recorder.close()

        if node.bag is not None:
            node.bag.reap(wait=True)

        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
