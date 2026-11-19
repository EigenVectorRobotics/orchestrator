"""Mission Control: one node that serves the page and turns a click into the ROS message agent_cli would send.

It runs where the executor runs (the NUC), with a small web server on 127.0.0.1:8080. From the laptop:

    ssh -L 8080:localhost:8080 vector@vector-nuc        # then open the link the node logs, with its token

It reads the state (executor status, open task, step request, recorder, trace) and publishes a task, a step
decision and a cancel. It never calls a skill's own interface. The state and the rules of a command are in
mission_control.py; the page is in the package's web/ folder, read once at start.
"""

import json
import time
import secrets
import mimetypes
import threading

from pathlib import Path
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import rclpy

from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from skill_registry import REMOVED, agent_dir, agent_knowledge_dir, skills_dir, stamps_file
from std_msgs.msg import Empty, String
from sensor_msgs.msg import Image
from visualization_msgs.msg import ImageMarker
from nav_msgs.msg import Odometry, Path as RoutePath
from std_msgs.msg import Int32
from skill_registry.geometry import pose_dict

from .qos import COMMAND_QOS, LATCHED_QOS, SENSOR_QOS, TRACE_QOS
from .topics import (CANCEL_TOPIC, LIVE_SKILLS_TOPIC, OPEN_TOPIC, OUTLINE_TOPIC, RECORDER_TOPIC, STATUS_TOPIC, STEP_DECISION_TOPIC, STEP_REPLY_TOPIC,
                     STEP_REQUEST_TOPIC, TASK_TOPIC, TRACE_TOPIC)
from .images import Camera, Outlines
from .map_view import MapView
from .map_places import MapInfoPlaces
from .mission_control import (DECISION_WAIT, Commands, KnowledgeShelf, MissionApi, MissionState, RunShelf, SkillShelf,
                              SessionFollower, StaleSkills, live_names, token_ok)
from .recording import RecorderConfig
from .session_log import CODEX_SESSIONS, SessionLog


MAX_BODY  = 64 * 1024
MATCH_WAIT = 2.0                        # seconds to wait until the executor is matched to the decision publisher


def share_web() -> str:
    """The page as the package installs it (setup.py); the launch file passes this as web_dir"""
    return str(Path(get_package_share_directory('orchestrator')) / 'web')


def read_web(directory: Path) -> dict[str, tuple[bytes, str]]:
    """The page and what it asks for, by name, read once: they do not change while the node runs"""
    try:
        files = {file.name: (file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                 for file in sorted(directory.iterdir()) if file.is_file()}
    except OSError as error:
        raise FileNotFoundError(f'no page in {directory} ({error}); build the workspace again, so that web/ is installed') from error

    if 'index.html' not in files:
        raise FileNotFoundError(f'no index.html in {directory}; build the workspace again, so that web/ is installed')

    return files


class MissionControlNode(Node):
    def __init__(self):
        super().__init__('mission_control')

        host    = self.declare_parameter('host', '127.0.0.1').value      # localhost only: reach it over ssh -L
        port    = self.declare_parameter('port', 8080).value
        token   = self.declare_parameter('token', '').value or secrets.token_urlsafe(16)   # 'off': no token
        off     = token == 'off'
        web_dir = Path(self.declare_parameter('web_dir', '').value or share_web()).expanduser()

        image_topic   = self.declare_parameter('image_topic', '/camera/camera/color/image_raw').value
        self.state    = MissionState(keep=self.declare_parameter('trace_keep', 400).value)
        self.camera   = Camera(image_topic, max_side=self.declare_parameter('camera_max_side', 960).value, quality=80)
        self.outlines = Outlines()
        self.map_view = MapView()
        self._live    = None                         # the live skills, from registry_node; None until it says
        self._outline_subscription = None            # only while the page shows the masks
        self._waiting = {}                           # step id -> (Event, [reply]) for each decision in flight
        self._lock    = threading.Lock()

        self._task_publisher     = self.create_publisher(String, TASK_TOPIC, COMMAND_QOS)
        self._decision_publisher = self.create_publisher(String, STEP_DECISION_TOPIC, COMMAND_QOS)
        self._cancel_publisher   = self.create_publisher(Empty, CANCEL_TOPIC, COMMAND_QOS)
        self._subscriptions      = [
            self.create_subscription(String, STATUS_TOPIC, self._reader('status'), LATCHED_QOS),
            self.create_subscription(String, OPEN_TOPIC, self._reader('task'), LATCHED_QOS),
            self.create_subscription(String, STEP_REQUEST_TOPIC, self._reader('step'), LATCHED_QOS),
            self.create_subscription(String, RECORDER_TOPIC, self._reader('recorder'), LATCHED_QOS),
            self.create_subscription(String, TRACE_TOPIC, self._reader('trace'), TRACE_QOS),
            self.create_subscription(String, STEP_REPLY_TOPIC, self._reader('step_reply'), COMMAND_QOS),
            self.create_subscription(Image, image_topic, self.camera.put, SENSOR_QOS),
            self.create_subscription(String, LIVE_SKILLS_TOPIC, self._on_live, LATCHED_QOS),
            self.create_subscription(Odometry, self.declare_parameter('pose_topic', '/odom_base_link').value,
                                     self._on_pose, SENSOR_QOS),
            self.create_subscription(Int32, self.declare_parameter('floor_topic', '/slam_elevatorlio/floor').value,
                                     lambda message: self.map_view.put_floor(message.data), LATCHED_QOS),
            self.create_subscription(RoutePath, self.declare_parameter('route_topic', '/route_planner/path').value,
                                     lambda message: self.map_view.put_route([(stamped.pose.position.x, stamped.pose.position.y,
                                                                               stamped.pose.position.z)
                                                                              for stamped in message.poses]), LATCHED_QOS),
        ]

        knowledge_dir      = self.declare_parameter('knowledge_dir', str(agent_knowledge_dir())).value
        library            = self.declare_parameter('skills_dir', str(skills_dir())).value
        agent_folders      = self.declare_parameter('agent_folders', [str(agent_dir()), str(agent_dir().parent / 'agent_discovery')]).value
        self.session_log   = SessionLog(agent_folders, self.declare_parameter('session_logs', str(CODEX_SESSIONS)).value)
        self.api           = MissionApi(self.state, Commands(self._send_task, self._decide_step, self._send_cancel),
                                       self.camera, token_needed=not off, outlines=self.outlines,
                                       skills=SkillShelf(library,
                                                         self.declare_parameter('stamps_file', str(stamps_file())).value,
                                                         self.declare_parameter('removed_dir', str(agent_dir() / REMOVED)).value,
                                                         stale=StaleSkills(knowledge_dir, library), live=lambda: self._live),
                                       knowledge=KnowledgeShelf(knowledge_dir),
                                       runs=RunShelf(self.declare_parameter('runs_root', RecorderConfig.root).value),
                                       map_view=self.map_view, session_log=self.session_log)
        self._places = self._map_places(self.declare_parameter('map_info', '/map_info').value)
        self.create_timer(1.0, self._follow_outlines)
        self.create_timer(2.0, self._watch_live)
        files              = read_web(web_dir)       # before the port is bound: a missing page is not half a server
        self.server        = ThreadingHTTPServer((host, port), Handler)
        self.server.api    = self.api
        self.server.files  = files
        self.server.token  = token
        self.server.log    = self.get_logger()
        self._web          = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._web.start()
        self._stop         = threading.Event()
        self._follower     = SessionFollower(self.state, self.session_log)
        threading.Thread(target=self._follow_session, daemon=True).start()   # not on the spin thread: a long log takes a while
        link = f'http://{host}:{port}/' + ('' if off else f'?t={token}')
        self.get_logger().info(f'mission control on {link} (page from {web_dir})')

        if off:
            self.get_logger().warning('token off: anything that reaches this port can drive the robot')
        else:
            self.get_logger().info('open that link, with its token: a command without the token is refused')

    # ========== State in ==========

    def _on_pose(self, message: Odometry):
        if self.map_view.wants_pose():               # 50-200 Hz in, about 5 Hz kept: the spin thread stays free
            self.map_view.put_pose(pose_dict(message))

    def _map_places(self, prefix: str):
        """The places of map_info, as the executor reads them; nothing when there is no map_info here"""
        try:
            places = MapInfoPlaces(self, prefix, without='the small map shows no places')
        except ImportError:                         # navigation_interfaces is not built here
            self.get_logger().warn('no navigation_interfaces: the map shows no places')
            return None

        self.create_timer(2.0, lambda: self.map_view.put_places(places()))
        return places

    def _on_live(self, message: String):
        self._live = live_names(message.data)

        if self._live is None:
            self.get_logger().warn(f'{LIVE_SKILLS_TOPIC}: not a list of skill names: {message.data[:200]!r}')

    def _watch_live(self):
        """A live list from a registry_node that is gone says nothing: the latched message stays after it stops"""
        if self.count_publishers(LIVE_SKILLS_TOPIC) == 0:
            self._live = None

    def _follow_session(self):
        while not self._stop.wait(1.0):
            try:
                self._follower()
            except Exception as error:                  # a bad log must not stop the feed for the rest of the run
                self.get_logger().warn(f'cannot read the agent\'s session log: {error!r}', throttle_duration_sec=60.0)

    def _follow_outlines(self):
        """Subscribe to the mask outlines while a page asks for them, and stop when no page has asked for a while"""
        wanted = self.outlines.wanted()

        if wanted and self._outline_subscription is None:
            self._outline_subscription = self.create_subscription(ImageMarker, OUTLINE_TOPIC, self.outlines.put, SENSOR_QOS)
        elif not wanted and self._outline_subscription is not None:
            self.destroy_subscription(self._outline_subscription)
            self._outline_subscription = None
            self.outlines.clear()

    def _reader(self, part: str):
        """One JSON callback per topic: the trace keeps every event, a step reply goes to the thread that waits"""
        sink = {'trace': self.state.trace, 'step_reply': self._on_step_reply}.get(part, partial(self.state.put, part))

        def read(message: String):
            try:
                sink(json.loads(message.data))
            except json.JSONDecodeError as error:
                self.get_logger().warn(f'{part}: not JSON ({error})')

        return read

    def _on_step_reply(self, reply: dict):
        with self._lock:
            waiting = self._waiting.get(reply.get('id'))

        if waiting is not None:
            waiting[1].append(reply)
            waiting[0].set()

    # ========== Commands out ==========

    def _send_task(self, request: dict) -> bool:
        if not self._matched(self._task_publisher):
            return False

        self._task_publisher.publish(String(data=json.dumps(request)))

        return True

    def _send_cancel(self) -> bool:
        if not self._matched(self._cancel_publisher):
            return False

        self._cancel_publisher.publish(Empty())

        return True

    def _matched(self, publisher) -> bool:
        """A command is volatile: it is lost until the executor is matched to the publisher"""
        deadline = time.monotonic() + MATCH_WAIT

        while publisher.get_subscription_count() == 0:
            if time.monotonic() > deadline:
                return False

            time.sleep(0.05)

        return True

    def _decide_step(self, decision: dict) -> dict | None:
        """Publish one step decision and wait for the executor's reply; None if it never came"""
        if not self._matched(self._decision_publisher):
            return None

        event, replies = threading.Event(), []

        with self._lock:
            self._waiting[decision['id']] = (event, replies)

        try:
            self._decision_publisher.publish(String(data=json.dumps(decision)))
            event.wait(DECISION_WAIT)
        finally:
            with self._lock:
                self._waiting.pop(decision['id'], None)

        return replies[0] if replies else None

    def shutdown(self):
        self._stop.set()
        self.server.shutdown()
        self.server.server_close()
        self._web.join(timeout=2.0)


class Handler(BaseHTTPRequestHandler):
    """/api/* goes to the adapter; every other path is one of the files read at start"""

    server_version   = 'MissionControl'
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        url = urlparse(self.path)

        if url.path == '/api/camera.jpg':
            frame = self.server.api.camera.frame()

            return self._send(200, *frame) if frame else self._json(503, {'ok': False, 'message': 'no camera frame yet'})

        if url.path.startswith('/api/'):
            return self._json(*self.server.api.get(url.path, {key: value[0] for key, value in parse_qs(url.query).items()}))

        name = 'index.html' if url.path in ('/', '') else url.path.lstrip('/')
        file = self.server.files.get(name)

        if file is None:
            return self._json(404, {'ok': False, 'message': f'no such file {name}'})

        self._send(200, *file)

    def do_POST(self):
        if not self._from_this_page():
            return self._refused(403, 'a command comes from the Mission Control page, as JSON')

        if not token_ok(self.server.token, self.headers.get('X-Mission-Token')):
            return self._refused(403, 'wrong token: open the page from the link in the node log')

        length = int(self.headers.get('Content-Length') or 0)

        if length > MAX_BODY:
            return self._refused(413, 'the body is too long')

        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError as error:
            return self._json(400, {'ok': False, 'message': f'not JSON: {error}'})

        self._json(*self.server.api.post(urlparse(self.path).path, body))

    def _from_this_page(self) -> bool:
        """While the ssh tunnel is open, any page in the operator's browser can reach this port. A form post
        cannot set this content type, and a page on another site cannot hide its origin"""
        origin = self.headers.get('Origin')
        local  = ('127.0.0.1', 'localhost')

        return (self.headers.get('Content-Type', '').split(';')[0].strip() == 'application/json'
                and self.headers.get('Host', '').rsplit(':', 1)[0].strip('[]') in local
                and (origin is None or urlparse(origin).hostname in local))

    def _refused(self, code: int, message: str):
        """A refusal that leaves the body unread. The connection closes, because the body it did not read must not
        be read as the next request on a kept-alive socket"""
        self.close_connection = True
        self._json(code, {'ok': False, 'message': message})

    def _json(self, code: int, payload: dict):
        if self.command == 'POST' and code >= 400:   # log_message is quiet: a refused command must still be seen
            self.server.log.warning(f'refused POST {urlparse(self.path).path} ({code}): {payload.get("message")}')

        self._send(code, json.dumps(payload, default=str).encode(), 'application/json')

    def _send(self, code: int, body: bytes, media_type: str):
        self.send_response(code)
        self.send_header('Content-Type', media_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, template, *arguments):
        pass                                        # one line per poll, every second: the node's own log is enough


def main():
    rclpy.init()
    node = MissionControlNode()

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
