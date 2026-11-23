"""Record every orchestrator run on the NUC (see recorder.py); refuse runs when the disk is too full.

Subscribes to the trace, the task and the agent topics; records a bag of the chosen topics for each tree run;
publishes /orchestrator/recorder/status (latched JSON: ready, reason, free_gb, session). The executor refuses a
tree while the status is not ready, when its require_recorder is true.
"""

import json
import time
import signal
import subprocess

from pathlib import Path
from functools import partial

import rclpy

from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import String

from ..common.topics import (AGENT_EVENT_TOPIC as EVENT_TOPIC, AGENT_REPLY_TOPIC as REPLY_TOPIC, AGENT_REQUEST_TOPIC as REQUEST_TOPIC, COMMAND_QOS, LATCHED_QOS,
                     RECORDER_STATUS_TOPIC as STATUS_TOPIC, SPEAK_TOPIC, TASK_TOPIC, TRACE_QOS, TRACE_TOPIC)
from ..execution.node_setup import declare_sources
from .recorder import STATE_DIR, Recorder, RecorderConfig, directory_digests, file_digest, find_repos, git_state, largest_runs, state_digests


BAG_STOP_S   = 10.0                             # SIGINT, then this long to close the file, then SIGKILL

BAG_TOPICS = [                                  # the bag_topics parameter's default; the one list (config leaves it out)
    TRACE_TOPIC, REQUEST_TOPIC, REPLY_TOPIC, EVENT_TOPIC,
    '/rosout',                                              # every node's log, so the run keeps what orchestrator.log shows
    SPEAK_TOPIC, '/route_goal', '/route_planner/status', '/nav_state', '/state_estimation', '/odom_base_link',
    '/track_3d/is_tracking', '/track_3d/selected_target_3d', '/slam_elevatorlio/status',
    '/track_3d/selected_normal_pose',                       # the button pose track_3d fits (raw)
    '/track_3d/selected_normal_pose_filtered/arm_upright',  # the pose the press plans to (filtered)
    # unitree_interface (names after its launch remaps): the robot's state and the commands sent to it
    '/feedback',                                            # joint states from rt/lowstate, about 1 kHz
    '/pelvis_imu', '/torso_imu',
    '/unitree_interface/current_mode', '/unitree_interface/fsm_id',
    '/position_control',                                    # joint commands (cmd_arm and cmd_low both map here)
    '/cmd_vel',                                             # the cmd_vel_mux output unitree_interface sends
    '/navigation_cmd_vel', '/manual_rotate_cmd_vel', '/local_movement_cmd_vel', '/safety_cmd_vel',
    '/teleop_cmd_vel',                                      # the cmd_vel_mux inputs (navigation_stack cmd_vel_mux.yaml)
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
        capture_dir = self.declare_parameter('capture_dir', RecorderConfig.capture_dir).value
        repo_roots  = self.declare_parameter('repo_roots', ['/home/vector/orchestrator_ws/src', '/home/vector/eigen_vector_ws/src']).value
        record_bag  = self.declare_parameter('bag', True).value
        bag_topics  = self.declare_parameter('bag_topics', BAG_TOPICS).value
        state_dir   = self.declare_parameter('state_dir', str(STATE_DIR)).value

        skill_dirs, places_file, _ = declare_sources(self)

        provenance = {
            'git':    [git_state(repo) for repo in find_repos(repo_roots)],
            'skills': {str(directory): directory_digests(directory, '*/*') for directory in skill_dirs},
            'places': {'path': places_file, 'sha256': file_digest(Path(places_file))},
            'state':  {'path': state_dir, 'files': state_digests(Path(state_dir))},     # what skills keep across runs
        }

        self.bag      = RosBag(self, list(bag_topics)) if record_bag else None
        self.recorder = Recorder(RecorderConfig(root=root, min_free_gb=min_free_gb, capture_dir=capture_dir), provenance, self.bag)

        self._status_publisher = self.create_publisher(String, STATUS_TOPIC, LATCHED_QOS)
        self._subscriptions    = [       # not self._subscriptions of Node: that name is taken by rclpy
            self.create_subscription(String, TRACE_TOPIC, self._json(self.recorder.on_event), TRACE_QOS),
            self.create_subscription(String, TASK_TOPIC, lambda message: self.recorder.on_task(message.data), 10),
            self.create_subscription(String, REQUEST_TOPIC, self._json(self.recorder.on_agent_request), LATCHED_QOS),
            self.create_subscription(String, REPLY_TOPIC, self._json(partial(self.recorder.on_agent_message, 'reply')), COMMAND_QOS),
            self.create_subscription(String, EVENT_TOPIC, self._json(partial(self.recorder.on_agent_message, 'event')), COMMAND_QOS),
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
