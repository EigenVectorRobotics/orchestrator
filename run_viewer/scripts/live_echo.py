"""Print the nav state with the arm goal (2 Hz), and each agent request and reply, as JSON lines for the Run Viewer map and agent panes."""

import json
import math
import sys
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.task import Future
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from geometry_msgs.msg import PointStamped, PoseStamped, Quaternion
from nav_msgs.msg import Odometry, Path
from rcl_interfaces.srv import GetParameters
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2
from std_msgs.msg import Int32, String


PERIOD_S      = 0.5
MAX_ROUTE     = 400
MAX_OBSTACLES = 800
MAX_TERRAIN   = 1500
MAX_FREE      = 1500
PLANNER       = '/local_planner'
ARM_TRAJ      = '/traj'                             # move_to_pose's planned end effector path; its last pose is the arm goal
ARM_TF_S      = 30.0                                # TF is read only this long after an arm plan: /tf is a heavy stream on the NUC
PARAMS        = {'goal_clear_range': 0.1, 'adjacent_range': 3.5, 'obstacle_height_threshold': 0.1, 'path_crop_by_goal': True}
LATCHED       = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
ANY           = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
COMMAND       = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
TRACE         = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)


def yaw_of(
    q: Quaternion,
) -> float:
    """Yaw of a quaternion message."""
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def rotate(
    q: Quaternion,
    v: list,
) -> list:
    """Vector v rotated by quaternion q."""
    u = np.array([q.x, q.y, q.z])
    v = np.array(v, dtype=float)
    return list(v + 2.0 * np.cross(u, np.cross(u, v) + q.w * v))


def thin(
    points: list,
    limit: int,
) -> list:
    """Keep at most limit points, evenly spaced, the last one always."""
    if len(points) <= limit:
        return points
    step = len(points) / limit
    return [points[int(i * step)] for i in range(limit - 1)] + [points[-1]]


class LiveEcho(Node):
    """Keep the latest nav messages, print the nav state on a timer and the agent topics on arrival."""

    def __init__(self) -> None:
        """Subscribe to the nav and agent topics and ask the local planner for its parameters."""
        super().__init__('run_viewer_live_echo')
        self.floor   = None
        self.pose    = None
        self.route   = []
        self.goal    = None
        self.goal_at = 0.0
        self.local   = []
        self.terrain = None
        self.free    = None
        self.params  = dict(PARAMS)
        self.got_params = False
        self.create_subscription(Int32, '/slam_elevatorlio/floor', self.on_floor, ANY)
        self.create_subscription(Odometry, '/odom_base_link', self.on_odom, ANY)
        self.create_subscription(Path, '/route_planner/path', self.on_route, LATCHED)
        self.create_subscription(PointStamped, '/way_point', self.on_way_point, ANY)
        self.create_subscription(PoseStamped, '/goal_pose', self.on_goal_pose, ANY)
        self.create_subscription(Path, '/path', self.on_path, ANY)
        self.create_subscription(PointCloud2, '/terrain_map', self.on_terrain, ANY)
        self.create_subscription(PointCloud2, '/free_paths', self.on_free, ANY)
        self.create_subscription(String, '/orchestrator/agent/request', self.on_request, LATCHED)
        self.create_subscription(String, '/orchestrator/agent/reply', self.on_reply, COMMAND)
        self.arm     = None                   # (last pose of /traj, its frame, when it came)
        self.arm_map = None                   # that goal in map, from the last TF lookup
        self.tf      = None
        self.tf_listener = None               # made at an arm plan, dropped ARM_TF_S after it
        self.create_subscription(Path, ARM_TRAJ, self.on_traj, ANY)
        if '--trace' in sys.argv:            # the trace from the topic, e.g. from a recorder bag played back, in place of events.jsonl
            self.create_subscription(String, '/orchestrator/trace', self.on_trace, TRACE)
        self.client = self.create_client(GetParameters, f'{PLANNER}/get_parameters')
        self.create_timer(PERIOD_S, self.emit_nav)
        self.create_timer(5.0, self.ask_params)

    def on_floor(
        self,
        message: Int32,
    ) -> None:
        """The floor the robot is localized on."""
        self.floor = message.data

    def on_odom(
        self,
        message: Odometry,
    ) -> None:
        """The robot pose in map."""
        p = message.pose.pose
        self.pose = [p.position.x, p.position.y, p.position.z, yaw_of(p.orientation)]

    def on_route(
        self,
        message: Path,
    ) -> None:
        """The route planner path in map."""
        self.route = thin([[round(s.pose.position.x, 2), round(s.pose.position.y, 2)] for s in message.poses], MAX_ROUTE)

    def on_way_point(
        self,
        message: PointStamped,
    ) -> None:
        """The local planner goal from the route planner."""
        self.goal, self.goal_at = [message.point.x, message.point.y], time.time()

    def on_goal_pose(
        self,
        message: PoseStamped,
    ) -> None:
        """The local planner goal from a goal pose."""
        self.goal, self.goal_at = [message.pose.position.x, message.pose.position.y], time.time()

    def on_path(
        self,
        message: Path,
    ) -> None:
        """The local planner path, in base_link."""
        self.local = [[s.pose.position.x, s.pose.position.y] for s in message.poses]

    def on_terrain(
        self,
        message: PointCloud2,
    ) -> None:
        """The terrain map in map; intensity is the height above the ground."""
        self.terrain = message

    def on_free(
        self,
        message: PointCloud2,
    ) -> None:
        """The candidate paths the collision grid left clear, in base_link."""
        self.free = message

    def on_request(
        self,
        message: String,
    ) -> None:
        """Print the parts of an agent request that the agent pane shows: its id, its task and the path of its failure."""
        request = json.loads(message.data)
        failure = request['failure'] if isinstance(request.get('failure'), dict) else {}
        print(json.dumps({'name': 'agent_request', 'time': time.time(), 'id': request.get('id'), 'task': request.get('task'),
                          'failure': {'path': failure.get('path'), 'message': failure.get('message')}}, default=str), flush=True)

    def on_traj(
        self,
        message: Path,
    ) -> None:
        """The arm goal: the last pose of a planned end effector path (an empty path keeps the last goal); start TF for it."""
        if message.poses:
            self.arm, self.arm_map = (message.poses[-1].pose, message.header.frame_id, time.time()), None
            if self.tf_listener is None:
                self.tf = Buffer()
                self.tf_listener = TransformListener(self.tf, self)

    def arm_in_map(self) -> dict | None:
        """The arm goal in map: position, and its x axis in the floor plane (the arrow); after TF stops, the last one found."""
        if self.arm is None:
            return None
        pose, frame, at = self.arm
        age = time.time() - at
        if self.tf_listener is not None and age > ARM_TF_S:
            self.tf_listener.unregister()
            self.tf_listener = None
        if self.tf_listener is not None:
            try:
                t = self.tf.lookup_transform('map', frame, Time()).transform
            except TransformException:
                t = None
            if t is not None:
                p = pose.position
                x, y, z = (np.array(rotate(t.rotation, [p.x, p.y, p.z])) + [t.translation.x, t.translation.y, t.translation.z]).tolist()
                axis = rotate(t.rotation, rotate(pose.orientation, [1.0, 0.0, 0.0]))
                self.arm_map = {'x': round(x, 3), 'y': round(y, 3), 'z': round(z, 3), 'dx': round(axis[0], 3), 'dy': round(axis[1], 3), 'frame': frame}
        return {**self.arm_map, 'age_s': round(age, 1)} if self.arm_map else None

    def on_trace(
        self,
        message: String,
    ) -> None:
        """Print one trace event as it came."""
        print(json.dumps({'name': 'trace', 'raw': json.loads(message.data)}), flush=True)

    def on_reply(
        self,
        message: String,
    ) -> None:
        """Print the executor's reply to an agent decision."""
        print(json.dumps({'name': 'agent_reply', 'time': time.time(), **json.loads(message.data)}, default=str), flush=True)

    def ask_params(self) -> None:
        """Ask the local planner for the parameters of its collision check, until it answers once."""
        if self.got_params or not self.client.service_is_ready():
            return
        future = self.client.call_async(GetParameters.Request(names=list(PARAMS)))
        future.add_done_callback(self.on_params)

    def on_params(
        self,
        future: Future,
    ) -> None:
        """Keep the parameter values the local planner gave."""
        for name, value in zip(PARAMS, future.result().values):
            given = {1: value.bool_value, 2: value.integer_value, 3: value.double_value}.get(value.type)
            if given is not None:
                self.params[name] = given
        self.got_params = True

    def emit_nav(self) -> None:
        """Print the nav state: floor, pose, route, goal, check circle, obstacles in it, local path, all in map."""
        if self.pose is None:
            return
        x, y, _, yaw = self.pose
        p = self.params
        goal_m = math.hypot(self.goal[0] - x, self.goal[1] - y) if self.goal else None
        radius = p['adjacent_range']
        if goal_m is not None and p['path_crop_by_goal']:
            radius = min(radius, goal_m + p['goal_clear_range'])
        obstacles, terrain = [], []
        if self.terrain is not None:            # numpy, not a loop per point: the terrain map has tens of thousands of points
            px, py, h = point_cloud2.read_points_numpy(self.terrain, field_names=('x', 'y', 'intensity'), skip_nans=True).astype(float).T
            d = np.hypot(px - x, py - y)
            tall = (h > p['obstacle_height_threshold']) & (d < p['adjacent_range'])
            xy = np.round(np.stack([px, py], axis=1), 2)
            obstacles, terrain = xy[tall & (d < radius)].tolist(), xy[tall & (d >= radius)].tolist()
        c, s = math.cos(yaw), math.sin(yaw)
        local = [[round(x + c * u - s * v, 2), round(y + s * u + c * v, 2)] for u, v in self.local]
        free = []
        if self.free is not None:
            u, v = point_cloud2.read_points_numpy(self.free, field_names=('x', 'y'), skip_nans=True).astype(float).T
            free = np.round(np.stack([x + c * u - s * v, y + s * u + c * v], axis=1), 2).tolist()
        print(json.dumps({
            'name': 'nav', 'time': time.time(), 'floor': self.floor, 'pose': [round(v, 3) for v in self.pose],
            'route': self.route, 'goal': self.goal, 'goal_age_s': round(time.time() - self.goal_at, 1) if self.goal else None,
            'goal_m': goal_m, 'radius': radius, 'obstacles': thin(obstacles, MAX_OBSTACLES), 'terrain': thin(terrain, MAX_TERRAIN),
            'free': thin(free, MAX_FREE), 'local': local, 'arm': self.arm_in_map(), 'params': p,
            'params_from': 'planner' if self.got_params else 'defaults',
        }), flush=True)


def main() -> None:
    """Spin until killed."""
    rclpy.init()
    rclpy.spin(LiveEcho())


if __name__ == '__main__':
    main()
