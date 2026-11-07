"""Hand-made task chain for the FVD elevator task (Test 10): the baseline for the orchestrator's LLM-planned trees.

python3 task_executor.py --ros-args -p places:=/path/fvd_sim_places.yaml
ros2 topic pub --once /task_executor/command std_msgs/msg/String "{data: 'start floor:1; stop floor:2; target: door 1'}"

Steps: localize, walk to the call panel, press the call button, enter when the door opens, press the floor button,
ride, exit when the door opens, relocalize, walk to the target. Each step has a timeout and bounded retries.
In and out of the car the robot moves like a person: right after a press it steps back while the arms release and turns
to its next walk, then walks straight through the door and turns to the panel (no planners in the 1.2 m door, few
waypoints, wide tolerances except at a press pose).
It uses the robot's interfaces only (the same names as the orchestrator's skill contracts); the status goes to
/task_executor/status as JSON.
"""
import json
import math
import os
import re
import sys
import threading
import time

import numpy as np
import rclpy
import yaml

from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, TwistStamped
from humanoid_manipulation_interfaces.action import VisualServoingArm
from nav_msgs.msg import Odometry, Path
from rclpy.action import ActionClient
from rcl_interfaces.srv import SetParameters
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import Bool, Float32, String
from std_srvs.srv import Trigger
from unitree_interface_msgs.srv import SetHandPose

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "door_state"))
from utils import count_points_in_rectangle  # noqa: E402

LATCHED_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
COMMAND_QOS = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
# Status details of a move in progress (logged at most every 2 s): planners, final approach, the executor's own moves
MOVING = ("walking:", "aligning:", "stepping back:", "turning:", "walking straight:")


class TaskFailed(RuntimeError):
    pass


class DoorClosed(TaskFailed):
    pass


def parse_command(
    text: str,
) -> tuple[str, str, str]:
    """'start floor:2; stop floor:1; target: door 1' -> ('2', '1', 'door 1')."""
    fields = dict(re.findall(r"(start|stop|target)\s*(?:floor)?\s*:\s*([^;]+)", text.lower()))
    if set(fields) != {"start", "stop", "target"}:
        raise TaskFailed(f"command needs start, stop and target: '{text}'")

    return fields["start"].strip(), fields["stop"].strip(), fields["target"].strip()


def wrap(
    angle: float,
) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


class TaskExecutor(Node):
    def __init__(self) -> None:
        super().__init__("task_executor")

        with open(self.declare_parameter("places", "").value) as file:
            self.places = yaml.safe_load(file)

        self.button_check = self.declare_parameter("button_check", "topic").value  # none | topic (sim stand-in) | vlm
        self.vlm_base_url = self.declare_parameter("vlm_base_url", "http://vector-thor:8090/v1").value
        self.max_press_attempts = self.declare_parameter("max_press_attempts", 3).value
        self.nav_timeout = self.declare_parameter("nav_timeout_s", 180.0).value
        self.door_timeout = self.declare_parameter("door_timeout_s", 40.0).value
        self.align_tolerance = self.declare_parameter("align_tolerance_m", 0.07).value
        self.align_yaw_tolerance = math.radians(self.declare_parameter("align_tolerance_deg", 5.0).value)
        self.sensor_height = self.declare_parameter("sensor_height_m", 1.23).value
        self.walk_speed = self.declare_parameter("walk_speed", 0.5).value
        self.door_speed = self.declare_parameter("door_speed", 0.4).value  # the straight walks in and out of the car
        self.step_back_distance = self.declare_parameter("step_back_m", 0.3).value  # after a hall press, before the turn
        self.car_step_back = self.declare_parameter("car_step_back_m", 0.6).value  # after the floor press: the walk out starts here
        self.stop_lead = self.declare_parameter("stop_lead_s", 0.3).value  # the gait walks on ~this long after a stop
        # nav: every move is a navigation stack goal (route planner, local planner) to one of five places: start, call
        # panel, car panel, exit, target. own: the executor's own moves near the elevator (step back, turn, straight
        # walk through the door, final approach)
        self.locomotion = self.declare_parameter("locomotion", "nav").value
        # YAML {node: {parameter: value}} set on the navigation stack at each command: its arrival tuned for the press poses
        self.nav_params = self.declare_parameter("nav_params", "").value
        # locomotion nav: the step back after a press. Under path_follower look_ahead_distance (0.5 m), or the follower
        # turns to the goal behind first and the raised arm sweeps the panel (run 36: floor 1 pressed)
        self.nav_step_back = self.declare_parameter("nav_step_back_m", 0.3).value
        # locomotion nav: speed of the step back and of the walk back to the press pose. /goal_reached comes as the robot
        # passes the goal; from 0.43 m/s the gait coasted 0.2 m past it, to the panel (run 37)
        self.nav_return_speed = self.declare_parameter("nav_return_speed", 0.2).value
        # Wait after release_arms before the body moves (the robot blends the arms back over 5 s): a step back with the
        # arm still out swung the gripper onto the button below (run 42: floor 1)
        self.release_wait = self.declare_parameter("release_wait_s", 5.0).value

        self.lock = threading.Lock()
        self.pose = None                 # base_link x, y, z, yaw in map
        self.pose_time = 0.0
        self.slam, self.slam_count, self.rejections = {}, 0, 0  # rejections: GICP matches rejected in a row
        self.pf, self.pf_count = {}, 0
        self.route = {}
        self.goal_reached = False
        self.doors = {"landing": None, "car": None}
        self.door_since = {"landing": 0.0, "car": 0.0}
        self.door_opened = {"landing": 0.0, "car": 0.0}  # last time the door had read OPEN for 1 s
        self.door_closed = {"landing": 0.0, "car": 0.0}  # last time the door had read CLOSED for 1 s
        self.floor_label, self.floor_count = None, 0
        self.before_press = (0, None, 0.0)  # IMU floor count and label, time of the press
        self.lit = {}
        self.lit_seen = {}  # button -> last time it read lit
        self.image = None
        self.status = {"state": "idle"}
        self.last_log = 0.0
        self.command = None

        self.goal_pub = self.create_publisher(PoseStamped, "/route_goal", COMMAND_QOS)
        self.cancel_pub = self.create_publisher(Bool, "/cancel_goal", COMMAND_QOS)
        self.initial_pub = self.create_publisher(PoseWithCovarianceStamped, "/initialpose", 1)
        self.pf_pub = self.create_publisher(PoseWithCovarianceStamped, "/pf_relocalize/request", 1)
        self.autonomy_pub = self.create_publisher(Bool, "/autonomy_mode", 5)
        self.speed_pub = self.create_publisher(Float32, "/speed", 5)
        self.move_pub = self.create_publisher(TwistStamped, "/local_movement_cmd_vel", 5)
        self.status_pub = self.create_publisher(String, "/task_executor/status", LATCHED_QOS)
        self.goal_viz_pub = self.create_publisher(PoseStamped, "/task_executor/goal", LATCHED_QOS)  # route or align goal
        self.plan_viz_pub = self.create_publisher(Path, "/task_executor/plan", LATCHED_QOS)  # the line of an own move

        self.create_subscription(String, "/task_executor/command", self.on_command, 10)
        self.create_subscription(Odometry, "/odom_base_link", self.on_odom, 10)
        self.create_subscription(String, "/slam_elevatorlio/status", self.on_slam, LATCHED_QOS)
        self.create_subscription(String, "/route_planner/status", self.on_route, LATCHED_QOS)
        self.create_subscription(String, "/pf_relocalize/status", self.on_pf, 1)
        self.create_subscription(Bool, "/goal_reached", self.on_goal_reached, 5)
        self.create_subscription(String, "/door_state/landing", lambda message: self.on_door("landing", message), 10)
        self.create_subscription(String, "/door_state/car", lambda message: self.on_door("car", message), 10)
        self.create_subscription(String, "/elevator/floor", self.on_floor_label, LATCHED_QOS)
        if self.button_check == "topic":
            self.create_subscription(String, "/sim/button_lit", self.on_lit, 10)
        if self.button_check == "vlm":
            self.create_subscription(CompressedImage, "/camera/camera/color/image_raw/compressed", self.on_image, 1)

        self.arm = ActionClient(self, VisualServoingArm, "humanoid_arm_visual_servoing")
        self.release_client = self.create_client(Trigger, "/unitree_interface/release_arms")
        self.hand_client = self.create_client(SetHandPose, "/unitree_interface/set_hand_pose")

        self.publish_status("idle", "waiting for a command on /task_executor/command")
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()

    # ========== Inputs ==========

    def on_command(
        self,
        message: String,
    ) -> None:
        with self.lock:
            if self.status["state"] == "running":
                self.get_logger().warn(f"busy, command ignored: {message.data}")
                return
            self.command = message.data

    def on_odom(
        self,
        message: Odometry,
    ) -> None:
        pose = message.pose.pose
        yaw = 2.0 * math.atan2(pose.orientation.z, pose.orientation.w)
        with self.lock:
            self.pose = np.array([pose.position.x, pose.position.y, pose.position.z, wrap(yaw)])
            self.pose_time = time.monotonic()

    def on_slam(
        self,
        message: String,
    ) -> None:
        with self.lock:
            self.slam = json.loads(message.data)
            self.slam_count += 1
            self.rejections = self.rejections + 1 if self.slam.get("error", "").startswith("match rejected") else 0

    def on_pf(
        self,
        message: String,
    ) -> None:
        with self.lock:
            self.pf, self.pf_count = json.loads(message.data), self.pf_count + 1

    def on_route(
        self,
        message: String,
    ) -> None:
        with self.lock:
            self.route = json.loads(message.data)

    def on_goal_reached(
        self,
        message: Bool,
    ) -> None:
        with self.lock:
            self.goal_reached = self.goal_reached or message.data

    def on_door(
        self,
        name: str,
        message: String,
    ) -> None:
        with self.lock:
            if message.data != self.doors[name]:
                self.door_since[name] = time.monotonic()
            self.doors[name] = message.data
            if message.data == "OPEN" and time.monotonic() - self.door_since[name] > 1.0:
                self.door_opened[name] = time.monotonic()
            if message.data == "CLOSED" and time.monotonic() - self.door_since[name] > 1.0:
                self.door_closed[name] = time.monotonic()

    def on_floor_label(
        self,
        message: String,
    ) -> None:
        with self.lock:
            self.floor_label, self.floor_count = message.data, self.floor_count + 1

    def on_lit(
        self,
        message: String,
    ) -> None:
        with self.lock:
            self.lit = json.loads(message.data)
            self.lit_seen.update({button: time.monotonic() for button, on in self.lit.items() if on})

    def on_image(
        self,
        message: CompressedImage,
    ) -> None:
        self.image = bytes(message.data)

    # ========== Helpers ==========

    def publish_status(
        self,
        state: str,
        detail: str = "",
        step: str = "",
    ) -> None:
        """Latched JSON status; also the trace in the log."""
        changed = (state, step or self.status.get("step")) != (self.status.get("state"), self.status.get("step"))
        self.status = {"state": state, "step": step or self.status.get("step", ""), "detail": detail,
                       "command": self.status.get("command", ""), "time": round(time.monotonic(), 1)}
        self.status_pub.publish(String(data=json.dumps(self.status)))
        if changed or time.monotonic() - self.last_log > 2.0 or not detail.startswith(MOVING):
            self.get_logger().info(f"[{self.status['step']}] {state}: {detail}")
            self.last_log = time.monotonic()

    def wait_for(
        self,
        condition,
        timeout: float,
        period: float = 0.05,
    ) -> bool:
        """Poll condition() until true or timeout [s]."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(period)

        return condition()

    def floor(
        self,
        name: str,
    ) -> dict:
        if name not in self.places["floors"]:
            raise TaskFailed(f"floor {name} is not in the places file")

        return self.places["floors"][name]

    def pose_on(
        self,
        floor: str,
        pose: dict,
    ) -> dict:
        """A places entry (x, y, yaw) with the floor's ground z."""
        return {**pose, "z": self.floor(floor)["ground_z"]}

    @staticmethod
    def behind(
        pose: dict,
        distance: float,
    ) -> np.ndarray:
        """x, y of the point `distance` [m] behind a places pose (where a step back from it ends)."""
        return np.array([pose["x"] - distance * math.cos(pose["yaw"]), pose["y"] - distance * math.sin(pose["yaw"])])

    def current(self) -> np.ndarray:
        with self.lock:
            if self.pose is None or time.monotonic() - self.pose_time > 5.0:
                raise TaskFailed("no fresh /odom_base_link")
            return self.pose.copy()

    def distance_to(
        self,
        pose: dict,
    ) -> tuple[float, float]:
        """Planar distance [m] and heading error [rad] of the robot to a pose."""
        current = self.current()

        return math.hypot(pose["x"] - current[0], pose["y"] - current[1]), wrap(pose["yaw"] - current[3])

    def tune_navigation(
        self,
        values: dict,
    ) -> None:
        """Set parameters of navigation nodes ({node: {parameter: value}}) through their services; they take them at once."""
        for target, parameters in values.items():
            client = self.create_client(SetParameters, f"/{target}/set_parameters")
            if not client.wait_for_service(timeout_sec=10.0):
                raise TaskFailed(f"no parameter service on {target}")
            future = client.call_async(SetParameters.Request(
                parameters=[Parameter(name, value=value).to_parameter_msg() for name, value in parameters.items()]))
            if not self.wait_for(future.done, 10.0):
                raise TaskFailed(f"{target} did not answer the parameter request")
            refused = [f"{name}: {result.reason}" for name, result in zip(parameters, future.result().results)
                       if not result.successful]
            if refused:
                raise TaskFailed(f"{target} refused {refused}")
            self.destroy_client(client)

    def set_autonomy(self) -> None:
        self.autonomy_pub.publish(Bool(data=True))

    def stop(self) -> None:
        """Drop the route goal and send zero on the local movement input."""
        self.cancel_pub.publish(Bool(data=True))
        self.move_pub.publish(self.twist(0.0, 0.0, 0.0))

    def pose_stamped(
        self,
        goal: dict,
    ) -> PoseStamped:
        """A places pose with z in the map frame."""
        message = PoseStamped()
        message.header.frame_id, message.header.stamp = "map", self.get_clock().now().to_msg()
        message.pose.position.x, message.pose.position.y, message.pose.position.z = goal["x"], goal["y"], goal["z"]
        message.pose.orientation.z, message.pose.orientation.w = math.sin(goal["yaw"] / 2), math.cos(goal["yaw"] / 2)

        return message

    def publish_plan(
        self,
        end: np.ndarray,
        start: np.ndarray | None = None,
    ) -> None:
        """The straight line of an own move, from start (default: the robot) to end (x, y), on /task_executor/plan (for
        the video)."""
        current = self.current()
        message = Path()
        message.header.frame_id, message.header.stamp = "map", self.get_clock().now().to_msg()
        for x, y in (current[:2] if start is None else start, end):
            pose = PoseStamped()
            pose.header = message.header
            pose.pose.position.x, pose.pose.position.y, pose.pose.position.z = float(x), float(y), float(current[2])
            message.poses.append(pose)
        self.plan_viz_pub.publish(message)

    def twist(
        self,
        forward: float,
        left: float,
        yaw_rate: float,
    ) -> TwistStamped:
        message = TwistStamped()
        message.header.stamp, message.header.frame_id = self.get_clock().now().to_msg(), "base_link"
        message.twist.linear.x, message.twist.linear.y, message.twist.angular.z = forward, left, yaw_rate

        return message

    def settle(
        self,
        seconds: float = 1.5,
    ) -> None:
        """Wait until the base has not moved for `seconds` (a press needs a still robot)."""
        last, still_since = self.current(), time.monotonic()
        end = time.monotonic() + 15.0
        while time.monotonic() < end and time.monotonic() - still_since < seconds:
            time.sleep(0.1)
            now = self.current()
            if np.linalg.norm(now[:2] - last[:2]) > 0.01 or abs(wrap(now[3] - last[3])) > 0.01:
                still_since = time.monotonic()
            last = now

    # ========== Skills ==========

    def localize(
        self,
        floor: str,
        pose: dict,
    ) -> None:
        """Initial guess -> GICP (slam_elevatorlio, +-30 deg search); if it does not match, particle filter -> GICP."""
        for attempt in range(1, 3):
            self.publish_status("running", f"initial guess ({pose['x']:.2f}, {pose['y']:.2f}, "
                                           f"{math.degrees(pose['yaw']):.0f} deg) on floor {floor}, attempt {attempt}")
            if self.match(self.initial_pub, floor, pose, (0.0, 0.0)):
                return

        # Heading unknown (the lobby's near-symmetry gives a 180 deg local optimum, which the filter's fit check rejects)
        for attempt in range(1, 3):
            self.publish_status("running", f"no GICP match from the guess: particle filter around it, attempt {attempt}")
            with self.lock:
                count = self.pf_count
            if self.match(self.pf_pub, floor, pose, (1.5, math.pi)) or (
                    self.pf_count > count and self.pf.get("state") == "done" and self.wait_localized(30.0)):
                return

        raise TaskFailed(f"no relocalization: GICP {self.slam.get('error')}; particle filter {self.pf}")

    def match(
        self,
        publisher,
        floor: str,
        pose: dict,
        sigmas: tuple[float, float],
    ) -> bool:
        """Send a guess (x, y sigma [m], yaw sigma [rad]) to /initialpose or the particle filter; wait for a match."""
        message = PoseWithCovarianceStamped()
        message.header.frame_id, message.header.stamp = "map", self.get_clock().now().to_msg()
        position = message.pose.pose.position
        position.x, position.y = float(pose["x"]), float(pose["y"])
        position.z = float(self.floor(floor)["ground_z"] + self.sensor_height)
        message.pose.pose.orientation.z = math.sin(pose["yaw"] / 2)
        message.pose.pose.orientation.w = math.cos(pose["yaw"] / 2)
        message.pose.covariance[0] = message.pose.covariance[7] = sigmas[0] ** 2
        message.pose.covariance[35] = sigmas[1] ** 2
        publisher.publish(message)

        return self.wait_localized(30.0)

    def wait_localized(
        self,
        timeout: float,
    ) -> bool:
        """A new SLAM status that says localized and healthy within timeout; no after 4 rejected matches (~4 s)."""
        with self.lock:
            count = self.slam_count
        localized = lambda: self.slam_count > count and self.slam.get("localized") and self.slam.get("healthy")
        if self.wait_for(lambda: localized() or (self.slam_count > count + 4 and self.rejections >= 4), timeout) \
                and localized():
            self.publish_status("running", f"localized: structure inliers {self.slam.get('structure_inlier_ratio')}")
            return True

        return False

    def navigate(
        self,
        floor: str,
        pose: dict,
        precise: bool = False,
        speed: float | None = None,
        abort_if=None,
        tolerance: tuple[float, float] | None = None,
    ) -> None:
        """Route goal on the prior map at a speed [m/s]; done on local_planner /goal_reached.

        precise (locomotion own): the planners stop within 0.3 m, so they go to a pre-pose 0.6 m behind the goal and the
        final approach walks the rest. Within 1.5 m the planners are skipped (a goal behind the robot stalls them:
        two_way_drive). precise with locomotion nav: the planners go to the step back point behind the goal, then
        straight to the goal at the return speed; the same last leg as a new press after a step back. A goal approached
        across its heading is circled, and /goal_reached came as the robot backed through it (run 41: 0.14 m off).
        """
        goal = self.pose_on(floor, pose)
        if precise and self.locomotion == "own":
            pre = {**goal, "x": goal["x"] - 0.6 * math.cos(goal["yaw"]), "y": goal["y"] - 0.6 * math.sin(goal["yaw"])}
            if self.distance_to(goal)[0] > 1.5:
                self.navigate(floor, pre, speed=speed, abort_if=abort_if)
            self.align(goal, tolerance)
            self.settle()
            return

        if precise:
            x, y = self.behind(goal, self.nav_step_back)
            current = self.current()
            if math.hypot(x - current[0], y - current[1]) > 0.15:  # not already there after a step back
                self.navigate(floor, {**pose, "x": float(x), "y": float(y)}, speed=speed, abort_if=abort_if)
            speed = self.nav_return_speed

        message = self.pose_stamped(goal)
        with self.lock:
            self.goal_reached, self.route = False, {}
        self.set_autonomy()
        time.sleep(0.1)
        self.speed_pub.publish(Float32(data=float(speed or self.walk_speed)))
        self.goal_pub.publish(message)
        self.goal_viz_pub.publish(message)

        start, blocked_since = time.monotonic(), None
        while time.monotonic() - start < self.nav_timeout:
            time.sleep(0.2)
            with self.lock:
                reached, state = self.goal_reached, self.route.get("state")
            distance, heading = self.distance_to(goal)
            if reached and distance < 0.4:
                break
            if abort_if is not None and abort_if():
                self.stop()
                raise DoorClosed("the door closed")
            if "diverged" in self.slam.get("error", ""):
                self.stop()
                raise TaskFailed(f"localization lost: {self.slam['error']}")
            if state == "blocked":
                blocked_since = blocked_since or time.monotonic()
                if time.monotonic() - blocked_since > 20.0:
                    self.stop()
                    raise TaskFailed(f"route blocked for 20 s, {distance:.2f} m from the goal")
            else:
                blocked_since = None
            self.publish_status("running", f"walking: {distance:.2f} m, {math.degrees(heading):.0f} deg to go "
                                           f"(route {state})")
        else:
            self.stop()
            raise TaskFailed(f"not at the goal after {self.nav_timeout:.0f} s")

        # Drop the goal: path_follower would otherwise turn back to it when the robot moves on (fine approach)
        self.cancel_pub.publish(Bool(data=True))
        self.settle()

    def align(
        self,
        goal: dict,
        tolerance: tuple[float, float] | None = None,
        moves: int = 3,
        timeout: float = 15.0,
    ) -> None:
        """Final approach on /local_movement_cmd_vel (it outranks navigation in the mux): omnidirectional P control.

        Each move stops inside 0.7 x the tolerance, then the robot stands still before the next check (at most `moves`).
        A gait moves on a few cm after a stop: a loop that starts again on that at once cycled 0.2 m sideways (run 18).
        The speed stays in 0.15-0.25 m/s (the gait ignores less) and a move stops early by the gait's coast.
        """
        self.goal_viz_pub.publish(self.pose_stamped(goal))
        self.publish_plan(np.array([goal["x"], goal["y"]]))
        tolerance, yaw_tolerance = tolerance or (self.align_tolerance, self.align_yaw_tolerance)
        end = time.monotonic() + timeout
        # A big turn first, in place: turning while walking sideways is where the gait drifts most
        while time.monotonic() < end and abs(wrap(goal["yaw"] - self.current()[3])) > math.radians(20.0):
            self.move_pub.publish(self.twist(0.0, 0.0, math.copysign(0.4, wrap(goal["yaw"] - self.current()[3]))))
            time.sleep(0.05)
        self.move_pub.publish(self.twist(0.0, 0.0, 0.0))
        time.sleep(0.8)
        for move in range(1, moves + 1):
            distance, error = self.distance_to(goal)
            if distance < tolerance and abs(error) < yaw_tolerance:
                break
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                current = self.current()
                dx, dy = goal["x"] - current[0], goal["y"] - current[1]
                forward = math.cos(current[3]) * dx + math.sin(current[3]) * dy
                left = -math.sin(current[3]) * dx + math.cos(current[3]) * dy
                distance, error = math.hypot(dx, dy), wrap(goal["yaw"] - current[3])
                if distance < 0.7 * tolerance and abs(error) < 0.7 * yaw_tolerance:
                    break
                vx = vy = 0.0
                speed = min(0.25, max(0.15, 0.8 * distance))
                if distance >= 0.7 * tolerance and distance > self.stop_lead * speed:
                    vx, vy = speed * forward / distance, speed * left / distance
                yaw_rate = max(-0.4, min(0.4, 1.5 * error))
                if vx == vy == 0.0 and abs(yaw_rate) < 0.25:
                    yaw_rate = math.copysign(0.25, error)  # a slower turn alone is under the gait's dead band
                self.move_pub.publish(self.twist(vx, vy, yaw_rate))
                self.publish_status("running", f"aligning: move {move}, {distance:.3f} m, {math.degrees(error):.1f} deg")
                time.sleep(0.05)
            self.move_pub.publish(self.twist(0.0, 0.0, 0.0))
            self.settle(1.0)

        distance, error = self.distance_to(goal)
        self.publish_status("running", f"aligned to {distance:.3f} m, {math.degrees(error):.1f} deg")

    def turn_to(
        self,
        yaw: float,
        tolerance: float = math.radians(8.0),
        timeout: float = 15.0,
    ) -> None:
        """Turn in place on the local movement input until the heading is within tolerance [rad] of yaw."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and abs(error := wrap(yaw - self.current()[3])) > tolerance:
            rate = max(0.3, min(0.8, 1.5 * abs(error)))  # the gait stands for a turn alone under ~0.25 rad/s
            self.move_pub.publish(self.twist(0.0, 0.0, math.copysign(rate, error)))
            self.publish_status("running", f"turning: {math.degrees(error):.0f} deg to go")
            time.sleep(0.05)
        self.move_pub.publish(self.twist(0.0, 0.0, 0.0))

    def step_back(
        self,
        distance: float,
        speed: float = 0.25,
        timeout: float = 10.0,
    ) -> None:
        """Walk straight back `distance` m, heading held: the hand clears the panel before the robot turns."""
        start, end = self.current(), time.monotonic() + timeout
        self.publish_plan(start[:2] - distance * np.array([math.cos(start[3]), math.sin(start[3])]))
        while time.monotonic() < end:
            current = self.current()
            left = distance - math.hypot(*(current[:2] - start[:2]))
            if left < self.stop_lead * speed:
                break
            yaw_rate = max(-0.3, min(0.3, 1.5 * wrap(start[3] - current[3])))
            self.move_pub.publish(self.twist(-speed, 0.0, yaw_rate))
            self.publish_status("running", f"stepping back: {left:.2f} m to go")
            time.sleep(0.05)
        self.move_pub.publish(self.twist(0.0, 0.0, 0.0))

    def walk_straight(
        self,
        goal: dict,
        tolerance: float,
        abort_if=None,
        start: np.ndarray | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Turn to the goal point, then walk the straight line to it at door_speed; done within tolerance [m].

        The line runs from start (x, y; default: where the robot is), so a robot that ended its step back off the planned
        start walks onto the planned line before the door (run 27: 0.31 m off). Pure pursuit on the line (a point 0.4 m
        ahead on it): the sim gait walks ~10 deg left of its heading at 0.4 m/s, and steering at the end point alone let
        it drift 0.33 m off the line into a door jamb (run 22). The planners stall in the 1.2 m door (run 21: no clear
        local path 43 % of the time). Not the last cm to a press pose: align.
        """
        self.goal_viz_pub.publish(self.pose_stamped(goal))
        target = np.array([goal["x"], goal["y"]])
        current = self.current()
        self.turn_to(math.atan2(target[1] - current[1], target[0] - current[0]))
        start = self.current()[:2] if start is None else start
        self.publish_plan(target, start)
        length = float(np.linalg.norm(target - start))
        direction = (target - start) / max(length, 1e-6)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            current = self.current()
            along, distance = float((current[:2] - start) @ direction), float(np.linalg.norm(target - current[:2]))
            if distance < max(tolerance, self.stop_lead * self.door_speed) or along > length:
                break
            if abort_if is not None and abort_if():
                self.move_pub.publish(self.twist(0.0, 0.0, 0.0))
                raise DoorClosed("the door closed")
            carrot = start + min(along + 0.4, length) * direction
            bearing = wrap(math.atan2(carrot[1] - current[1], carrot[0] - current[0]) - current[3])
            self.move_pub.publish(self.twist(self.door_speed, 0.0, max(-0.5, min(0.5, 2.0 * bearing))))
            self.publish_status("running", f"walking straight: {distance:.2f} m to go")
            time.sleep(0.05)
        else:
            self.move_pub.publish(self.twist(0.0, 0.0, 0.0))
            raise TaskFailed(f"not at the straight walk's goal after {timeout:.0f} s")

        self.move_pub.publish(self.twist(0.0, 0.0, 0.0))

    def release_arms(self) -> None:
        """Give the arms back to the gait (the robot blends them back over 5 s); not waited for."""
        if self.release_client.wait_for_service(timeout_sec=2.0):
            self.release_client.call_async(Trigger.Request())

    def press(
        self,
        query: str,
    ) -> tuple[bool, str]:
        """POINT hand, one VisualServoingArm goal (the tracker query is inside the skill), then give the arms back.

        The query goes to Thor's grounding service as is, which takes only up, down and floor_N.
        POINT keeps the other fingers off the next button: cabin buttons are only 10 cm apart. The action returns with the
        hand on the button; then the release (5 s on the robot) is waited for.
        """
        if not self.arm.wait_for_server(timeout_sec=10.0):
            raise TaskFailed("no humanoid_arm_visual_servoing server")
        with self.lock:
            self.before_press = (self.floor_count, self.floor_label, time.monotonic())  # the car can leave at the touch
        if self.hand_client.wait_for_service(timeout_sec=2.0):
            hand = SetHandPose.Request(target_hand=SetHandPose.Request.HAND_RIGHT, target_pose=SetHandPose.Request.POSE_POINT)
            self.wait_for(self.hand_client.call_async(hand).done, 5.0)

        goal = VisualServoingArm.Goal(target_description=query, left_arm=False)
        sent = self.arm.send_goal_async(goal)
        if not self.wait_for(sent.done, 10.0) or not sent.result().accepted:
            return False, "goal rejected"

        result = sent.result().get_result_async()
        if not self.wait_for(result.done, 90.0):
            sent.result().cancel_goal_async()
            return False, "timeout"

        self.release_arms()
        time.sleep(self.release_wait)

        answer = result.result().result

        return answer.success, answer.message

    def reach(
        self,
        button: str,
        realign: dict,
        floor: str,
        attempts: int = 3,
    ) -> bool:
        """Align again until the annotated button is in its press region: the button's own region in the places file,
        else press_region (cabin buttons are 10 cm apart). In panel coordinates, as press_reach_sweep.py: base_link
        distance from the panel along its normal, the button to the right of a robot facing the panel, turn left still
        needed to face it. locomotion nav: back to the step back point and the last leg again; the arrival spreads
        +-5 cm (runs 42-44)."""
        target = self.places["buttons"][button]
        region = target.get("region", self.places["press_region"])
        facing = target["normal_yaw"] + math.pi
        for _ in range(attempts):
            current = self.current()
            dx, dy = target["x"] - current[0], target["y"] - current[1]
            distance = -(math.cos(target["normal_yaw"]) * dx + math.sin(target["normal_yaw"]) * dy)
            lateral = math.sin(facing) * dx - math.cos(facing) * dy
            turn = math.degrees(wrap(facing - current[3]))
            if (region["distance"][0] <= distance <= region["distance"][1] and region["lateral"][0] <= lateral <= region["lateral"][1]
                    and region["turn_deg"][0] <= turn <= region["turn_deg"][1]):
                return True
            self.publish_status("running", f"{button} {distance:.2f} m from the panel, {lateral:.2f} m right, {turn:.0f} deg: "
                                           f"outside the press region, approach again")
            if self.locomotion == "nav":
                x, y = self.behind(realign, self.nav_step_back)
                self.navigate(floor, {**realign, "x": float(x), "y": float(y)}, speed=self.nav_return_speed)
                self.navigate(floor, realign, precise=True)
                continue
            self.step_back(0.2)  # inside the align tolerance the final approach does not move (run 23)
            self.align(realign)
            self.settle()

        self.get_logger().warn(f"{button} still outside the press region: pressing anyway")
        return False

    def button_lit(
        self,
        button: str,
        description: str,
    ) -> bool | None:
        """Is the pressed button lit? VLM on Thor, the sim stand-in topic, or None (not checked)."""
        if self.button_check == "topic":  # lit since the press: the car at the floor serves the call and the light goes off (run 45)
            with self.lock:
                return True if self.lit_seen.get(button, 0.0) > self.before_press[2] else self.lit.get(button)

        if self.button_check == "vlm" and self.image is not None:
            from orchestrator.vlm_client import VlmClient, VlmClientConfig, VlmClientError
            try:
                answer = VlmClient(VlmClientConfig(base_url=self.vlm_base_url)).ask(
                    self.image, f"Is the {description} lit up?", ["lit", "not_lit", "unsure"])
            except VlmClientError as error:
                self.get_logger().warn(f"VLM: {error}")
                return None
            return {"lit": True, "not_lit": False}.get(answer)

        return None

    def press_until(
        self,
        button: str,
        description: str,
        verify,
        realign: dict,
        step_back: float,
        face: dict,
        back=None,
    ) -> None:
        """Press; at once step back while the arms release, check the light, turn to the next walk (face: a point), then
        wait for the elevator to respond; press again on any failure.

        No wait after the press: the car can open or leave at once, and the door holds only 15 s. With locomotion nav the
        robot steps back after a hall press (the arm is back after the release wait) and stays at the press pose in the
        car: the sim car rode and closed its door again during a step back (run 46). A new press walks to the step back
        point and the last leg again.
        """
        own = self.locomotion == "own"
        floor = next(label for label, place in self.places["floors"].items() if place["ground_z"] == realign["z"])
        for attempt in range(1, self.max_press_attempts + 1):
            self.reach(button, realign, floor)
            self.publish_status("running", f"pressing the {description}, attempt {attempt}")
            # The places file's grounding query for this panel, else the button's name (hall_up -> up, floor_2 stays)
            success, message = self.press(self.places["buttons"][button].get("query", button.removeprefix("hall_")))
            self.publish_status("running", f"arm: {message}")
            if own:
                self.step_back(step_back)
            elif not self.in_car():  # clear of the wall before the turn to the car (0.33 m at the call panel)
                x, y = self.behind(realign, self.nav_step_back)
                self.navigate(floor, {**realign, "x": float(x), "y": float(y)}, speed=self.nav_return_speed)
            # The arm's result is motion, not activation: a press that reports a collision can still light the button.
            # The panel is in view again
            lit = self.button_lit(button, description)
            if lit is False:  # a missed press waited door_timeout_s for nothing (run 44)
                self.publish_status("running", f"the {description} is not lit: press again")
            else:
                if own:
                    current = self.current()
                    self.turn_to(math.atan2(face["y"] - current[1], face["x"] - current[0]))
                if verify():
                    return
                self.publish_status("running", "the elevator did not respond: press again")
            if own and back is not None:
                back()
            elif own:
                self.align(realign)
                self.settle()
            else:  # the step back point first, then the last leg
                self.navigate(floor, realign, precise=True, speed=self.nav_return_speed)

        raise TaskFailed(f"the {description} did not work in {self.max_press_attempts} attempts")

    def door_opens(
        self,
        side: str,
        timeout: float,
        since: float | None = None,
    ) -> bool:
        """The lidar door state on this side has read OPEN for 1 s after `since` (default now), within timeout of it."""
        since = time.monotonic() if since is None else since
        self.publish_status("running", f"waiting for the door ({side} view)")

        return self.wait_for(lambda: self.door_opened[side] > since, max(0.0, since + timeout - time.monotonic()))

    def door_held(
        self,
        side: str,
        state: str,
        seconds: float = 1.0,
    ) -> bool:
        """The lidar door state on this side has read `state` for `seconds` (one scan can flicker)."""
        with self.lock:
            return self.doors[side] == state and time.monotonic() - self.door_since[side] > seconds

    def car_arrives(
        self,
        start: str,
        stop: str,
    ) -> bool:
        """After a floor press: the car door closes and opens again (the car served a call), and the IMU floor counter
        (elevator_states) agrees on the floor change if it reported one.

        The counter counts every vertical acceleration pair, also the ones of a walking robot, so only its change from
        the press counts, not its absolute label; and it can miss a ride. The robot steps back while the car leaves: the
        car's start opens the counter's transition, so its direction stays right (runs 26-29). The door events count from
        the press: the sim car closed, rode and opened again before the release wait and the step back ended (run 42).
        """
        labels = [str(label) for label in self.places["floor_labels"]]
        count, before, pressed = self.before_press
        self.publish_status("running", "waiting for the car door to close")
        if not self.wait_for(lambda: self.door_closed["car"] > pressed, 30.0):
            return False

        self.publish_status("running", f"riding to floor {stop}: waiting for the car door to open")
        if not self.wait_for(lambda: self.door_opened["car"] > self.door_closed["car"], 90.0):
            return False

        expected = labels.index(stop) - labels.index(start)
        if self.floor_count == count:
            self.get_logger().warn("the IMU floor counter reported no ride: the door cycle alone says arrived")
            return True
        moved = labels.index(self.floor_label) - labels.index(before)
        self.publish_status("running", f"IMU floor counter: {before} -> {self.floor_label} ({moved:+d}, expected {expected:+d})")

        return moved == expected

    def ride(
        self,
        stop: str,
    ) -> None:
        """At the stop floor: wait for the car door, then put the localization on this floor before walking out.

        Plain LIO sees a still car, so its height stays on the start floor: the guess is the current x, y and heading
        with this floor's height, and GICP against the prior map does the rest.
        """
        if not self.door_opens("car", self.door_timeout, since=self.before_press[2]):  # it can close again: exit_car opens it
            raise TaskFailed(f"the car door did not open on floor {stop}")

        current = self.current()
        self.publish_status("running", f"door open on floor {stop}: relocalize on this floor")
        self.localize(stop, {"x": current[0], "y": current[1], "yaw": current[3]})

    def in_car(self) -> bool:
        """The base is inside the car's floor rectangle (corners in the places file)."""
        current = self.current()
        point = np.array([[current[0], current[1], 0.0, 1.0]])

        return count_points_in_rectangle(point, np.array(self.places["car"]["corners"], dtype=float), 0.0) == 1

    def enter_car(
        self,
        floor: str,
        call_button: str,
        call_description: str,
    ) -> None:
        """Walk straight in to the inside point while the door is open, turn to the panel, final approach to the press
        pose; if the door closes first, call again. locomotion nav: the planners to the car panel press pose."""
        car = self.places["car"]
        closed = lambda: self.door_held("landing", "CLOSED") and not self.in_car()
        for attempt in range(1, 3):
            try:
                if self.locomotion == "nav":
                    self.navigate(floor, car["panel"], precise=True, speed=self.door_speed, abort_if=closed)
                else:
                    self.walk_straight(self.pose_on(floor, car["inside"]), 0.15, abort_if=closed,
                                       start=self.behind(self.floor(floor)["call_panel"], self.step_back_distance))
            except DoorClosed:
                self.publish_status("running", f"the door closed before the robot was in (attempt {attempt}): call again")
                self.call_elevator(floor, call_button, call_description)
                continue
            if self.in_car():
                if self.locomotion == "own":
                    self.turn_to(car["panel"]["yaw"])
                    self.navigate(floor, car["panel"], precise=True)
                return

        raise TaskFailed("could not get into the car")

    def call_elevator(
        self,
        floor: str,
        button: str,
        description: str,
    ) -> None:
        """Press the call button at the panel, step back, turn to the way in and wait there for the door to open."""
        landing = self.floor(floor)
        self.navigate(floor, landing["call_panel"], precise=True)
        self.press_until(button, description, lambda: self.door_opens("landing", self.door_timeout, since=self.before_press[2]),
                         self.pose_on(floor, landing["call_panel"]), self.step_back_distance,
                         self.pose_on(floor, self.places["car"]["inside"]),
                         lambda: self.navigate(floor, landing["call_panel"], precise=True))

    def exit_car(
        self,
        floor: str,
    ) -> None:
        """Walk straight out to the exit: the robot stepped back and turned to it right after the floor press. A closed
        door is opened with the floor button. locomotion nav: the planners from the car panel to the exit."""
        car, exit_point = self.places["car"], self.pose_on(floor, self.floor(floor)["exit"])
        closed = lambda: self.door_held("car", "CLOSED") and self.in_car()
        for attempt in range(1, 3):
            try:
                if self.locomotion == "nav":
                    self.navigate(floor, self.floor(floor)["exit"], speed=self.door_speed, abort_if=closed)
                else:
                    self.walk_straight(exit_point, 0.25, abort_if=closed, start=self.behind(car["panel"], self.car_step_back))
                return
            except DoorClosed:
                self.publish_status("running", f"the car door closed before the robot was out (attempt {attempt}): "
                                               f"press floor {floor} to open it")
                self.navigate(floor, car["panel"], precise=True, speed=self.door_speed)
                self.press_until(f"floor_{floor}", f"elevator floor {floor} button",
                                 lambda: self.door_opens("car", self.door_timeout, since=self.before_press[2]),
                                 self.pose_on(floor, car["panel"]),
                                 self.car_step_back, exit_point)

        raise TaskFailed("could not get out of the car")

    # ========== The chain ==========

    def work(self) -> None:
        """Wait for a command; run the chain once per command."""
        while rclpy.ok():
            with self.lock:
                command, self.command = self.command, None
            if command is None:
                time.sleep(0.1)
                continue

            self.status["command"] = command
            started = time.monotonic()
            try:
                self.run(command)
                self.publish_status("done", f"at the target in {time.monotonic() - started:.0f} s", "done")
            except TaskFailed as error:
                self.stop()
                self.publish_status("failed", str(error))

    def run(
        self,
        command: str,
    ) -> None:
        start, stop, target = parse_command(command)
        if start == stop:
            raise TaskFailed("start and stop floor are the same")
        direction = "up" if int(stop) > int(start) else "down"
        car, start_floor, stop_floor = self.places["car"], self.floor(start), self.floor(stop)
        if target not in stop_floor["targets"]:
            raise TaskFailed(f"no target '{target}' on floor {stop}")
        call_button = f"hall_{direction}"
        call_description = f"elevator {direction} call button"
        floor_button = f"floor_{stop}"
        floor_description = f"elevator floor {stop} button"

        self.publish_status("running", f"floor {start} -> {stop}, target {target}", "1 receive the command")
        if self.nav_params:
            with open(self.nav_params) as file:
                self.tune_navigation(yaml.safe_load(file))

        self.publish_status("running", "relocalize in the prior map", "2 localize")
        self.localize(start, start_floor["start"])

        self.publish_status("running", f"walk to the call panel, press {direction}", "3 press the call button")
        self.call_elevator(start, call_button, call_description)

        self.publish_status("running", "the door is open: walk in", "4 enter the elevator")
        self.enter_car(start, call_button, call_description)

        self.publish_status("running", f"press {stop}", "5 press the floor button")
        self.press_until(floor_button, floor_description, lambda: self.car_arrives(start, stop), self.pose_on(start, car["panel"]),
                         self.car_step_back, self.pose_on(stop, stop_floor["exit"]))  # ready to walk out during the ride

        self.publish_status("running", "detect the door, relocalize on the new floor", "6 exit the elevator")
        self.ride(stop)
        self.exit_car(stop)
        if not self.slam.get("healthy"):
            self.localize(stop, stop_floor["exit"])

        self.publish_status("running", f"walk to {target}", "7 navigate to the target")
        self.navigate(stop, stop_floor["targets"][target])


def main() -> None:
    """Spin until Ctrl-C; the chain runs on its own thread."""
    rclpy.init()
    node = TaskExecutor()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)

    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
