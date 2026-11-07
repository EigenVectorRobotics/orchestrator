"""Simulated Orin and lidar: the elevator_sim MuJoCo world behind the G1's ROS 2 interfaces, so the NUC stack runs unchanged.

PYTHONPATH must hold the elevator_sim checkout (see config/vector_pc.env).
python3 sim_orin.py --ros-args -p motion:=unitree_rl

Real robot -> here:
  livox_ros_driver2 (NUC)            /lidar/scan (CustomMsg), /imu/data     Mid-360 ray cast from the head, IMU from rigid-body motion
  realsense2_camera (Orin)           /camera/camera/color/*, aligned depth   g1_head render, 640x480 like orin.launch.py
  unitree_interface (Orin)           /cmd_vel (TwistStamped), /feedback      env.set_robot_velocity, joint states
  arm visual servoing (NUC + Orin)   humanoid_arm_visual_servoing action     button pose from track_3d as on the robot (/track_3d/reset, the query on
                                                                             /keyboard_input; Thor's seed_bbox + Cosmos seed, EdgeTAM mask + depth on this
                                                                             camera stream), or read from the sim (button_source:=sim); G1Pilot plan, DEX3
                                                                             contact; answers after the press with the hand on the button
  release arms (unitree_interface)   /unitree_interface/release_arms         the arm goes back along its approach path; the legs may walk
  VLM button check (Thor)            /sim/button_lit                         ring state read from the sim (stand-in)
  hand pose (unitree_interface)      /unitree_interface/set_hand_pose        answered, no effect: the hand is a closed Dex1-1
                                                                             gripper (elevator_sim scripts/prepare_dex1.py)
Operator tools for mapping: /sim/goto (privileged A* drive, world frame) and /sim/call (a person presses a call button).
For the video (tool/hand_chaining/render_run.py): /sim/replay (the full sim state) and /sim/arm_plan, /sim/arm_goal (the G1Pilot plan).
"""
import io
import re
import json
import math
import time
import threading

import mujoco
import numpy as np
import rclpy

from PIL import Image as PilImage
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.time import Time
from geometry_msgs.msg import PoseStamped, TwistStamped
from nav_msgs.msg import Odometry, Path
from sensor_msgs.msg import CameraInfo, CompressedImage, Image, Imu, JointState
from std_msgs.msg import Empty, Int8, String
from std_srvs.srv import Trigger

from elevator_sim import ElevatorEnvironment
from elevator_sim.agent.errors import SkillPreconditionError
from elevator_sim.agent.manipulation import G1Planner, PressController
from elevator_sim.agent.robot_backend import LocalNavigator
from humanoid_manipulation_interfaces.action import VisualServoingArm
from livox_ros_driver2.msg import CustomMsg
from unitree_interface_msgs.srv import SetHandPose

from sim_utils import (
    CUSTOM_POINT_DTYPE,
    matrix_quaternion_xyzw,
    mid360_directions,
    quaternion_wxyz_matrix,
    pack_custom_msg,
    point_acceleration,
    rpy_matrix,
    transform,
    yaw_of,
)

# URDF g1_29dof_with_hand_rev_1_0_ros.urdf mid360_joint: sensor -> torso_link
SENSOR_TO_TORSO = transform(rpy_matrix(0.031021, 0.029913, 0.035418), (-0.011312, 0.005899, -0.407630))
# Elevator-LIO sensor_unitree_g1.yaml: the Mid-360 is upside down, body (sensor) = lidar turned 180 deg about x
SENSOR_TO_LIDAR = transform(np.diag([1.0, -1.0, -1.0]), (0.0, 0.0, 0.0))
# imu_t_lidar: the lidar origin in the IMU frame; the IMU axes are the lidar axes
IMU_IN_LIDAR = -np.array([-0.011, -0.02329, 0.04412])
GRAVITY = 9.81
PHYSICS_STEPS_PER_TICK = 2
LIDAR_GEOM_GROUPS = np.array([1, 0, 1, 1, 1, 0], dtype=np.uint8)  # environment only: robot visuals are moved to group 1
BUTTON_PATTERNS = ((r"\bfloor[\s_]*1\b|\bfirst floor\b|\blabeled 1\b", "floor_1"),
                   (r"\bfloor[\s_]*2\b|\bsecond floor\b|\blabeled 2\b", "floor_2"),
                   (r"\bdown\b", "hall_down"), (r"\bup\b", "hall_up"))


def button_for(
    description: str,
) -> str | None:
    """Sim button id of a text query, most specific first."""
    text = description.lower()

    return next((button for pattern, button in BUTTON_PATTERNS if re.search(pattern, text)), None)


class RouteFollower:
    """Privileged A* drive for the mapping operator: turn to each waypoint, then walk; end at the goal yaw."""

    def __init__(
        self,
        env: ElevatorEnvironment,
        speed: float = 0.4,
    ) -> None:
        self.env, self.navigator, self.speed = env, LocalNavigator(env._robot_motion), speed
        self.route, self.goal_yaw, self.state, self.turning = [], 0.0, "idle", False

    def start(
        self,
        x: float,
        y: float,
        yaw: float,
    ) -> None:
        """Plan with the sim collision geometry from the current pelvis position."""
        route = self.navigator.path(self.env.data.xpos[self.env._pelvis_id, :2].copy(), np.array([x, y]))
        self.route, self.goal_yaw = ([np.asarray(point) for point in route], yaw) if route else ([], yaw)
        self.state, self.turning = ("routing" if route else "blocked"), False

    def update(self) -> None:
        """One velocity command toward the next waypoint; loose tolerances, as a gait overshoots small moves."""
        if self.state != "routing":
            return

        position = self.env.data.xpos[self.env._pelvis_id, :2]
        yaw = self.env._robot_motion.observe()["heading"]
        offset = self.route[0] - position
        distance = float(np.linalg.norm(offset))
        last = len(self.route) == 1

        if not last and distance < 0.25:
            self.route.pop(0)
            return

        if last and (distance < 0.2 or self.turning):
            self.turning = distance < 0.4
            yaw_error = math.atan2(math.sin(self.goal_yaw - yaw), math.cos(self.goal_yaw - yaw))
            if abs(yaw_error) < 0.1 or not self.turning:
                self.env.stop_robot()
                self.route, self.state, self.turning = [], "arrived" if self.turning else "routing", False
                if self.state == "routing":
                    self.route = [offset + position]
                return
            self.env.set_robot_velocity(0.0, 0.0, float(np.clip(1.5 * yaw_error, -0.6, 0.6)), timeout=0.1)
            return

        yaw_error = math.atan2(offset[1], offset[0]) - yaw
        yaw_error = math.atan2(math.sin(yaw_error), math.cos(yaw_error))
        if abs(yaw_error) > 0.3:
            self.env.set_robot_velocity(0.0, 0.0, float(np.clip(1.5 * yaw_error, -0.6, 0.6)), timeout=0.1)
            return

        forward = float(np.clip(distance, 0.15, self.speed))
        self.env.set_robot_velocity(forward, 0.0, float(np.clip(1.5 * yaw_error, -0.5, 0.5)), timeout=0.1)


class SimOrin(Node):
    def __init__(self) -> None:
        super().__init__("sim_orin")

        self.motion = self.declare_parameter("motion", "unitree_rl").value
        self.realtime_factor = self.declare_parameter("realtime_factor", 1.0).value
        self.lidar_points_per_second = self.declare_parameter("lidar_points_per_second", 100000).value
        self.lidar_hz = self.declare_parameter("lidar_hz", 10.0).value
        self.lidar_noise = self.declare_parameter("lidar_noise_m", 0.01).value
        self.camera_hz = self.declare_parameter("camera_hz", 5.0).value
        self.elevator_start_floor = self.declare_parameter("elevator_start_floor", 1).value
        self.door_hold = self.declare_parameter("door_hold_s", 15.0).value
        self.record_hz = self.declare_parameter("record_hz", 15.0).value  # /sim/replay rate: the video frame rate
        self.rng = np.random.default_rng(self.declare_parameter("seed", 1).value)
        # The sim's geometry guard zeroes the gait command when a swing foot grazes the car sill edge, and the robot
        # stumbles. The real G1 has no such guard, so it is off: contacts are left to the physics.
        self.motion_guard = self.declare_parameter("motion_guard", False).value
        # The sim gait zeroes its step clock input while it stands but keeps the clock running, so a walk starts at a
        # random point of the step cycle; from the wrong foot the first steps throw the robot up to 0.4 m to its left.
        # A real controller starts each walk the same way: restart the clock at phase 0.
        self.gait_phase_reset = self.declare_parameter("gait_phase_reset", True).value
        # Button pose for a press: "track_3d" (Thor's perception on this camera stream, as on the robot) or "sim" (truth)
        self.button_source = self.declare_parameter("button_source", "track_3d").value
        # Where the robot is put down: [x, y (m), yaw (rad)] in the world; the default [0.0] keeps the scene's start
        # (1.6, -2.0, 90 deg), which is the annotated start (the localization guess)
        robot_start = self.declare_parameter("robot_start", [0.0]).value

        self.lock = threading.RLock()
        self.env = ElevatorEnvironment(robot_motion=self.motion)
        self.model, self.data = self.env.model, self.env.data
        if len(robot_start) == 3:
            self._put_robot(*robot_start)
        if self.motion == "unitree_rl" and not self.motion_guard:
            motion = self.env._robot_motion
            motion._guarded_command = lambda: motion.command.copy()
        if self.motion == "unitree_rl" and self.gait_phase_reset:
            self._start_walks_at_phase_zero(self.env._robot_motion)
        self.arm_release = None  # after a press: the arm's way back, held at the button until release_arms
        self._hold_arm_after_press(self.env._robot_motion)
        self._setup_model()
        self._settle_elevator()

        self.follower = RouteFollower(self.env)
        self.command, self.command_time = np.zeros(3), -math.inf
        self.press = None
        self.open_since = None
        self.camera_poses = {}  # image stamp (sec, nanosec) -> (sim time, world <- camera_color_optical_frame)
        self.query_time, self.button_poses = math.inf, []  # track_3d poses (world) of the images after the last query

        self.lidar_pub = self.create_publisher(CustomMsg, "/lidar/scan", 5)
        self.imu_pub = self.create_publisher(Imu, "/imu/data", 200)
        self.color_pub = self.create_publisher(Image, "/camera/camera/color/image_raw", 1)
        self.compressed_pub = self.create_publisher(CompressedImage, "/camera/camera/color/image_raw/compressed", 1)
        self.depth_pub = self.create_publisher(Image, "/camera/camera/aligned_depth_to_color/image_raw", 1)
        # Thor's track_3d takes the compressed pair: raw frames do not get through DDS over tailscale
        self.depth_compressed_pub = self.create_publisher(
            CompressedImage, "/camera/camera/aligned_depth_to_color/image_raw/compressedDepth", 1)
        self.info_pub = self.create_publisher(CameraInfo, "/camera/camera/color/camera_info", 1)
        self.feedback_pub = self.create_publisher(JointState, "/feedback", 10)
        self.truth_pub = self.create_publisher(Odometry, "/sim/ground_truth", 10)
        self.sensor_truth_pub = self.create_publisher(Odometry, "/sim/sensor_truth", 10)
        self.state_pub = self.create_publisher(String, "/sim/state", 10)
        self.lit_pub = self.create_publisher(String, "/sim/button_lit", 10)
        self.goto_pub = self.create_publisher(String, "/sim/goto_status", 10)
        self.replay_pub = self.create_publisher(String, "/sim/replay", 30)
        self.arm_plan_pub = self.create_publisher(Path, "/sim/arm_plan", 1)
        self.arm_goal_pub = self.create_publisher(PoseStamped, "/sim/arm_goal", 1)
        self.track_reset_pub = self.create_publisher(Empty, "/track_3d/reset", 5)
        self.query_pub = self.create_publisher(String, "/keyboard_input", 5)
        self.button_pose_pub = self.create_publisher(PoseStamped, "/sim/button_pose", 10)  # track_3d's pose in the world

        self.create_subscription(TwistStamped, "/cmd_vel", self.on_cmd_vel, 10)
        self.create_subscription(PoseStamped, "/track_3d/selected_normal_pose", self.on_button_pose, 10)
        self.create_subscription(PoseStamped, "/sim/goto", self.on_goto, 10)
        self.create_subscription(Int8, "/sim/call", self.on_call, 10)
        self.create_service(Trigger, "/unitree_interface/release_arms", self.on_release_arms)
        self.create_service(SetHandPose, "/unitree_interface/set_hand_pose", self.on_hand_pose)
        self.arm_server = ActionServer(self, VisualServoingArm, "humanoid_arm_visual_servoing", self.execute_press,
                                       cancel_callback=lambda goal: CancelResponse.ACCEPT,
                                       callback_group=ReentrantCallbackGroup())

        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    # ========== Setup ==========

    def _setup_model(self) -> None:
        """Robot visuals to group 1 (still drawn) so lidar rays skip the robot; ids and frames used every tick."""
        pelvis = self.env._pelvis_id
        for geom in range(self.model.ngeom):
            body = int(self.model.geom_bodyid[geom])
            while body and body != pelvis:
                body = int(self.model.body_parentid[body])
            if body == pelvis and self.model.geom_group[geom] == 2:
                self.model.geom_group[geom] = 1

        self.torso = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "torso_link")
        self.head = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, "g1_head")
        self.rings = [spec["ring"] for spec in self.env._button_registry.values() if spec["ring"] >= 0]
        self.torso_to_lidar = np.linalg.inv(SENSOR_TO_TORSO) @ SENSOR_TO_LIDAR
        self.joint_names = self.env.observe()["robot"]["joint_names"]
        self.scan = []
        self.scan_start = 0.0
        self.velocity6, self.acceleration6 = np.zeros(6), np.zeros(6)

    def _settle_elevator(self) -> None:
        """Send the empty car to elevator_start_floor and close its doors before the clock starts."""
        if self.elevator_start_floor == 0:
            return

        self.env.request_floor(int(self.elevator_start_floor))
        for _ in range(60):
            self.env.step(500)
            if self.env.state == "open":
                break
        self.env.close_doors()
        self.env.step(2000)

    def _put_robot(
        self,
        x: float,
        y: float,
        yaw: float,
    ) -> None:
        """Move the standing robot's free joint (pelvis) to x, y, yaw before the clock starts."""
        address = self.model.jnt_qposadr[self.model.body_jntadr[self.env._pelvis_id]]
        self.data.qpos[address:address + 2] = x, y
        self.data.qpos[address + 3:address + 7] = math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)
        mujoco.mj_forward(self.model, self.data)

    @staticmethod
    def _start_walks_at_phase_zero(
        motion,
    ) -> None:
        """Wrap the gait update: when a walk command follows a stand, set the step clock so its next tick reads 0."""
        update, walking = motion.update, [False]

        def update_from_phase_zero() -> None:
            walk = np.linalg.norm(motion.command) >= 0.1  # the gait stands below this command size
            if walk and not walking[0]:
                motion._phase = -0.02 / 0.6  # the update adds one 0.02 s tick of the 0.6 s cycle before it reads it
            if motion.data.time + 1e-9 >= motion._next_inference:
                walking[0] = walk
            update()

        motion.update = update_from_phase_zero

    def _hold_arm_after_press(
        self,
        motion,
    ) -> None:
        """Wrap the gait update: after a press the arm holds at the button until release_arms, then goes back along its
        approach path while the legs balance or walk (the robot's release blends the arm back to the gait)."""
        update = motion.update

        def update_then_arm() -> None:
            update()
            release = self.arm_release
            if release is None:
                return
            clock = release["clock"] - (0.0 if release["since"] is None else self.data.time - release["since"])
            if clock <= 0.0:
                # Back at the start of the path: the gait's carry posture and servo gains have the arm again
                self.model.actuator_gainprm[release["actuators"]], self.model.actuator_biasprm[release["actuators"]] = release["gains"]
                self.arm_release = None
                return
            path, dt = release["path"], release["dt"]
            index = min(len(path) - 2, int(clock / dt))
            alpha = min(1.0, max(0.0, clock / dt - index))
            correction = release["correction"] * clock / release["clock"]  # the press's sway correction fades out
            if release["since"] is not None:
                correction = correction + self.release_sway(release, index, alpha) * clock / release["clock"]
            self.data.ctrl[release["actuators"]] = (1 - alpha) * path[index] + alpha * path[index + 1] + correction

        motion.update = update_then_arm

    def release_sway(
        self,
        release: dict,
        index: int,
        alpha: float,
    ) -> np.ndarray:
        """Joint correction that keeps the gripper's way back fixed to the car (or the world) while the base moves: the
        car's start shook the robot 2.4 cm toward the panel, and the gripper, going down, pressed floor 1 (run 46)."""
        if "tcp_car" not in release:  # the way back at the release start, in the car (or world) and pelvis frames
            scratch = mujoco.MjData(self.model)
            mujoco.mj_copyData(scratch, self.model, self.data)
            points = []
            for q in release["path"]:
                scratch.qpos[release["qpos"]] = q
                mujoco.mj_kinematics(self.model, scratch)
                points.append(scratch.site_xpos[release["site"]].copy())
            origin = self.data.xpos[release["car"]] if release["car"] >= 0 else np.zeros(3)
            rotation = self.data.xmat[self.env._pelvis_id].reshape(3, 3)
            release["tcp_pelvis"] = (np.array(points) - self.data.xpos[self.env._pelvis_id]) @ rotation
            release["tcp_car"] = np.array(points) - origin
            release["sway"] = np.zeros(len(release["actuators"]))
        origin = self.data.xpos[release["car"]] if release["car"] >= 0 else np.zeros(3)
        rotation = self.data.xmat[self.env._pelvis_id].reshape(3, 3)
        wanted = origin + (1 - alpha) * release["tcp_car"][index] + alpha * release["tcp_car"][index + 1]
        planned = self.data.xpos[self.env._pelvis_id] + rotation @ ((1 - alpha) * release["tcp_pelvis"][index]
                                                                    + alpha * release["tcp_pelvis"][index + 1])
        jac = np.zeros((3, self.model.nv))
        mujoco.mj_jacSite(self.model, self.data, jac, None, release["site"])
        jac = jac[:, release["dofs"]]
        step = jac.T @ np.linalg.solve(jac @ jac.T + 1e-4 * np.eye(3), wanted - planned)
        step *= min(1.0, PressController.MAX_CORRECTION / max(float(np.linalg.norm(step)), 1e-9))
        release["sway"] += 0.05 * (step - release["sway"])  # the press's smoothing

        return release["sway"]

    # ========== Inputs ==========

    def on_cmd_vel(
        self,
        message: TwistStamped,
    ) -> None:
        with self.lock:
            self.command = np.array([message.twist.linear.x, message.twist.linear.y, message.twist.angular.z])
            self.command_time = self.data.time

    def on_goto(
        self,
        message: PoseStamped,
    ) -> None:
        orientation = message.pose.orientation
        yaw = 2.0 * math.atan2(orientation.z, orientation.w)

        with self.lock:
            self.follower.start(message.pose.position.x, message.pose.position.y, yaw)
            state = self.follower.state

        self.get_logger().info(f"goto ({message.pose.position.x:.2f}, {message.pose.position.y:.2f}, "
                               f"{math.degrees(yaw):.0f} deg): {state}")
        self.goto_pub.publish(String(data=state))

    def on_call(
        self,
        message: Int8,
    ) -> None:
        with self.lock:
            self.env.request_floor(int(message.data))

        self.get_logger().info(f"a person called the car to floor index {message.data}")

    def on_hand_pose(
        self,
        request: SetHandPose.Request,
        response: SetHandPose.Response,
    ) -> SetHandPose.Response:
        """No hand pose to set: the sim hand is a closed Dex1-1 gripper (it was a DEX3 hand with POINT)."""
        response.success, response.message = True, "sim: closed Dex1-1 gripper, no hand pose"

        return response

    def on_release_arms(
        self,
        request: Trigger.Request,
        response: Trigger.Response,
    ) -> Trigger.Response:
        """Start the arm's way back from the button (the robot blends its arms back to the gait over 5 s)."""
        with self.lock:
            if self.arm_release is not None and self.arm_release["since"] is None:
                self.arm_release["since"] = float(self.data.time)
        response.success, response.message = True, "sim: arms released"

        return response

    def on_button_pose(
        self,
        message: PoseStamped,
    ) -> None:
        """A track_3d pose (camera frame, +x the normal out of the panel) to the world with the camera pose of its image;
        published for the video, kept for the press if the image came after the last query."""
        known = self.camera_poses.get((message.header.stamp.sec, message.header.stamp.nanosec))
        if known is None:
            return
        p, q = message.pose.position, message.pose.orientation
        pose = known[1] @ transform(quaternion_wxyz_matrix((q.w, q.x, q.y, q.z)), (p.x, p.y, p.z))
        if known[0] > self.query_time:
            self.button_poses.append((known[0], pose[:3, 3], pose[:3, 0]))

        world = PoseStamped()
        world.header.stamp, world.header.frame_id = message.header.stamp, "world"
        world.pose.position.x, world.pose.position.y, world.pose.position.z = map(float, pose[:3, 3])
        x, y, z, w = matrix_quaternion_xyzw(pose[:3, :3])
        world.pose.orientation.x, world.pose.orientation.y, world.pose.orientation.z, world.pose.orientation.w = x, y, z, w
        self.button_pose_pub.publish(world)

    # ========== Physics loop ==========

    def loop(self) -> None:
        """Step physics in sim time that tracks wall time; publish with stamps start + sim time."""
        self.wall_start = time.time() - self.data.time / self.realtime_factor
        next_lidar = next_camera = next_slow = next_replay = self.data.time
        self.scan_start = self.data.time

        try:
            while self.running and rclpy.ok():
                ahead = self.data.time / self.realtime_factor - (time.time() - self.wall_start)
                if ahead > 0:
                    time.sleep(min(ahead, 0.01))
                    continue

                with self.lock:
                    self._apply_commands()
                    self.env.step(PHYSICS_STEPS_PER_TICK)
                    self._doors()
                    self._cast_lidar()
                    imu = self._imu_message()
                    now = self.data.time

                    scan = None
                    if now >= next_lidar:
                        next_lidar += 1.0 / self.lidar_hz
                        scan = self._take_scan()

                    images = None
                    if now >= next_camera:
                        next_camera += 1.0 / self.camera_hz
                        images = (self.env.render("g1_head", width=1280, height=720),
                                  self.env.render("g1_head", width=1280, height=720, mode="depth"), self.camera_pose(), now)

                    replay = None
                    if now >= next_replay:
                        next_replay += 1.0 / self.record_hz
                        replay = self._replay_state()

                    slow = None
                    if now >= next_slow:
                        next_slow += 0.1
                        slow = self._slow_state()

                self.imu_pub.publish(imu)
                if scan is not None:
                    self.lidar_pub.publish(scan)
                if images is not None:
                    self._publish_images(*images)
                if replay is not None:
                    self.replay_pub.publish(String(data=replay))
                if slow is not None:
                    self._publish_slow(*slow)
        except Exception as error:
            self.get_logger().fatal(f"physics loop failed: {error!r}")
            raise
        finally:
            with self.lock:
                self.env.close()

    def stamp(self):
        """ROS time of the current sim time."""
        seconds = self.wall_start + self.data.time / self.realtime_factor

        return Time(nanoseconds=int(seconds * 1e9)).to_msg()

    def _apply_commands(self) -> None:
        """The privileged mapping drive wins; else /cmd_vel, held 1 s like unitree_interface's Move()."""
        if self.env.arm_controller is not None:
            self._update_press()
            return

        try:
            if self.follower.state == "routing":
                self.follower.update()
                if self.follower.state != "routing":
                    self.goto_pub.publish(String(data=self.follower.state))
            elif self.data.time - self.command_time < 1.0 and np.any(self.command != 0.0):
                self.env.set_robot_velocity(*self.command, timeout=0.05)
        except (RuntimeError, ValueError) as error:
            self.get_logger().warn(f"velocity command refused: {error}", throttle_duration_sec=2.0)

    def _doors(self) -> None:
        """Light curtain: a closing car opens again when the robot is in the doorway. An idle open car closes after
        door_hold_s when the doorway is clear."""
        x, y = self.data.xpos[self.env._pelvis_id, :2]
        in_doorway = abs(x) < 0.9 and -0.5 < y < 0.8
        floor = self.env.current_floor
        if self.env.state == "closing" and in_doorway and floor is not None:
            self.env.request_floor(floor)  # a call on the docked floor opens its doors again
            return

        if self.door_hold < 0 or self.env.state != "open" or self.env._requests:
            self.open_since = None
            return

        if self.open_since is None:
            self.open_since = self.data.time

        if self.data.time - self.open_since > self.door_hold and not in_doorway:
            self.env.close_doors()
            self.open_since = None

    def lidar_pose(self) -> np.ndarray:
        """World <- livox_frame."""
        torso = transform(self.data.xmat[self.torso].reshape(3, 3), self.data.xpos[self.torso])

        return torso @ self.torso_to_lidar

    def camera_pose(self) -> np.ndarray:
        """World <- camera_color_optical_frame (a MuJoCo camera looks down its -z with y up; the optical frame z ahead, y
        down)."""
        rotation = self.data.cam_xmat[self.head].reshape(3, 3) @ np.diag([1.0, -1.0, -1.0])

        return transform(rotation, self.data.cam_xpos[self.head])

    def _cast_lidar(self) -> None:
        """Cast this tick's share of the scan from the current lidar pose (motion distortion like the real one)."""
        count = int(self.lidar_points_per_second * PHYSICS_STEPS_PER_TICK * self.model.opt.timestep)
        pose = self.lidar_pose()
        directions = mid360_directions(count, self.rng)
        world = (directions @ pose[:3, :3].T).ravel()
        geom = np.zeros(count, dtype=np.int32)
        distance = np.zeros(count)
        mujoco.mj_multiRay(self.model, self.data, pose[:3, 3], world, LIDAR_GEOM_GROUPS, 1, -1, geom, distance,
                           None, count, 40.0)

        hit = (geom >= 0) & (distance > 0.1)
        ranges = distance[hit] + self.rng.normal(0.0, self.lidar_noise, hit.sum())
        points = np.zeros(hit.sum(), dtype=CUSTOM_POINT_DTYPE)
        xyz = directions[hit] * ranges[:, None]
        points["x"], points["y"], points["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        points["offset_time"] = int((self.data.time - self.scan_start) / self.realtime_factor * 1e9)
        points["reflectivity"] = np.clip(self.rng.normal(60.0, 15.0, hit.sum()), 10, 255).astype(np.uint8)
        points["line"] = self.rng.integers(0, 4, hit.sum())
        self.scan.append(points)

    def _take_scan(self) -> bytes:
        """The points cast since the last scan, as one serialized CustomMsg."""
        points = np.concatenate(self.scan) if self.scan else np.zeros(0, dtype=CUSTOM_POINT_DTYPE)
        stamp_ns = int((self.wall_start + self.scan_start / self.realtime_factor) * 1e9)
        self.scan, self.scan_start = [], self.data.time

        return pack_custom_msg(stamp_ns, "livox_frame", points)

    def _imu_message(self) -> Imu:
        """Livox IMU: specific force [g] and angular velocity [rad/s] in the lidar axes, at the IMU point."""
        mujoco.mj_objectVelocity(self.model, self.data, mujoco.mjtObj.mjOBJ_BODY, self.torso, self.velocity6, 0)
        mujoco.mj_objectAcceleration(self.model, self.data, mujoco.mjtObj.mjOBJ_BODY, self.torso, self.acceleration6, 0)
        pose = self.lidar_pose()
        lever = pose[:3, :3] @ IMU_IN_LIDAR + pose[:3, 3] - self.data.xpos[self.torso]
        force = point_acceleration(self.acceleration6[3:], self.velocity6[:3], self.acceleration6[:3], lever)
        force = pose[:3, :3].T @ force / GRAVITY + self.rng.normal(0.0, 0.001, 3)
        rate = pose[:3, :3].T @ self.velocity6[:3] + self.rng.normal(0.0, 0.0005, 3)

        message = Imu()
        message.header.stamp, message.header.frame_id = self.stamp(), "livox_frame"
        message.angular_velocity.x, message.angular_velocity.y, message.angular_velocity.z = map(float, rate)
        message.linear_acceleration.x, message.linear_acceleration.y, message.linear_acceleration.z = map(float, force)

        return message

    def _replay_state(self) -> str:
        """The state the video renders the scene from: qpos, qvel (the floor display reads the car speed), lit rings."""
        return json.dumps({"time": round(float(self.data.time), 4),
                           "qpos": np.round(self.data.qpos, 5).tolist(), "qvel": np.round(self.data.qvel, 4).tolist(),
                           "lit": [ring for ring in self.rings if self.model.geom_matid[ring] == self.env._ring_lit]})

    def _slow_state(self) -> tuple:
        """10 Hz snapshot for the joint states, ground truth and the sim status."""
        observation = self.env.observe()
        robot, elevator = observation["robot"], observation["elevator"]
        lit = {button: self._lit(button) for button in ("hall_up", "hall_down", "floor_1", "floor_2")}

        return robot, elevator, lit, self.lidar_pose() @ SENSOR_TO_LIDAR, self.stamp()

    def _lit(
        self,
        button: str,
    ) -> bool | None:
        try:
            ring = self.env._button_registry[self.env.button_info(button)["joint"].removesuffix("_joint")]["ring"]
        except ValueError:
            return None

        return bool(ring >= 0 and self.model.geom_matid[ring] == self.env._ring_lit)

    def _publish_slow(
        self,
        robot: dict,
        elevator: dict,
        lit: dict,
        sensor: np.ndarray,
        stamp,
    ) -> None:
        joints = JointState()
        joints.header.stamp = stamp
        joints.name = list(self.joint_names)
        joints.position = [float(value) for value in robot["joint_positions"]]
        joints.velocity = [float(value) for value in robot["joint_velocities"]]
        self.feedback_pub.publish(joints)

        truth = Odometry()
        truth.header.stamp, truth.header.frame_id, truth.child_frame_id = stamp, "world", "pelvis"
        position = truth.pose.pose.position
        position.x, position.y, position.z = map(float, robot["base_position"])
        w, x, y, z = map(float, robot["base_quaternion_wxyz"])
        orientation = truth.pose.pose.orientation
        orientation.x, orientation.y, orientation.z, orientation.w = x, y, z, w
        self.truth_pub.publish(truth)

        sensor_truth = Odometry()
        sensor_truth.header.stamp, sensor_truth.header.frame_id, sensor_truth.child_frame_id = stamp, "world", "sensor"
        position = sensor_truth.pose.pose.position
        position.x, position.y, position.z = map(float, sensor[:3, 3])
        orientation = sensor_truth.pose.pose.orientation
        orientation.x, orientation.y, orientation.z, orientation.w = matrix_quaternion_xyzw(sensor[:3, :3])
        self.sensor_truth_pub.publish(sensor_truth)

        state = {"time": round(float(self.data.time), 2), "elevator_floor": elevator["floor"],
                 "elevator_state": elevator["state"], "lift_z": round(elevator["position"], 3),
                 "doors": {group: [round(float(value), 2) for value in fractions]
                           for group, fractions in elevator["door_open_fractions"].items()},
                 "robot": [round(float(value), 3) for value in (*robot["base_position"], robot["heading"])],
                 "fallen": bool(robot.get("fallen", False)), "blocked": bool(robot["blocked"])}
        self.state_pub.publish(String(data=json.dumps(state)))
        self.lit_pub.publish(String(data=json.dumps(lit)))

    def _publish_images(
        self,
        rgb: np.ndarray,
        depth: np.ndarray,
        camera: np.ndarray,
        rendered: float,
    ) -> None:
        """The robot's stream (bringup orin.launch.py: colour 640x480, depth aligned to it): the D435i colour sensor is
        16:9, so 640x480 is its centre 4:3 crop, 55.6 x 43.1 deg. Renders come in at 1280x720 (69.4 x 42.5 deg nominal).
        The camera pose of each image is kept by its stamp, to place the track_3d poses of that image."""
        stamp = self.stamp()
        self.camera_poses[(stamp.sec, stamp.nanosec)] = (rendered, camera)
        if len(self.camera_poses) > 100:
            del self.camera_poses[min(self.camera_poses)]
        frame = "camera_color_optical_frame"
        rgb = np.asarray(PilImage.fromarray(rgb[:, 160:1120]).resize((640, 480), PilImage.BILINEAR))
        depth = depth[:, 160:1120][np.arange(480) * 3 // 2][:, np.arange(640) * 3 // 2]
        native = self.env.camera_intrinsics("g1_head", width=1280, height=720)
        fx, fy = native["fx"] * 2 / 3, native["fy"] * 2 / 3
        cx, cy = (native["cx"] - 160 + 0.5) * 2 / 3 - 0.5, (native["cy"] + 0.5) * 2 / 3 - 0.5

        color = Image(height=480, width=640, encoding="rgb8", step=640 * 3)
        color.header.stamp, color.header.frame_id, color.data = stamp, frame, rgb.tobytes()
        self.color_pub.publish(color)

        millimeters = np.where((depth > 0.1) & (depth < 10.0), depth * 1000.0, 0.0).astype(np.uint16)
        aligned = Image(height=480, width=640, encoding="16UC1", step=640 * 2)
        aligned.header.stamp, aligned.header.frame_id, aligned.data = stamp, frame, millimeters.tobytes()
        self.depth_pub.publish(aligned)

        buffer = io.BytesIO()
        PilImage.fromarray(millimeters).save(buffer, format="PNG", compress_level=1)
        # image_transport's compressedDepth: a 12-byte header (unused for 16UC1), then the PNG
        packed = CompressedImage(format="16UC1; compressedDepth png", data=bytes(12) + buffer.getvalue())
        packed.header.stamp, packed.header.frame_id = stamp, frame
        self.depth_compressed_pub.publish(packed)

        buffer = io.BytesIO()
        PilImage.fromarray(rgb).save(buffer, format="JPEG", quality=95)  # the frame Cosmos and EdgeTAM get on Thor
        compressed = CompressedImage(format="jpeg", data=buffer.getvalue())
        compressed.header.stamp, compressed.header.frame_id = stamp, frame
        self.compressed_pub.publish(compressed)

        info = CameraInfo(height=480, width=640, distortion_model="plumb_bob", d=[0.0] * 5)
        info.header.stamp, info.header.frame_id = stamp, frame
        info.k = [fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0]
        info.p = [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0]
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        self.info_pub.publish(info)

    # ========== Arm: press a button ==========

    def execute_press(
        self,
        goal_handle,
    ) -> VisualServoingArm.Result:
        """Plan with G1Pilot to the button pose from track_3d (or read from the sim), then approach and press with DEX3
        contact. Answer after the press hold with the hand on the button, as the robot's skill does; release_arms brings
        the arm back."""
        result = VisualServoingArm.Result()
        button = button_for(goal_handle.request.target_description)

        if button is None:
            goal_handle.abort()
            result.message = f"no sim button matches '{goal_handle.request.target_description}'"
            return result

        estimate = None
        if self.button_source == "track_3d":
            estimate = self.track_button(goal_handle)
            if estimate is None:
                goal_handle.abort()
                result.message = f"tracking_failed: no track_3d pose for '{goal_handle.request.target_description}'"
                self.get_logger().warn(f"press {button}: {result.message}")
                return result

        with self.lock:
            try:
                info = self.env.button_info(button)
                truth = np.asarray(info["position"]), np.asarray(info["normal"])
                position, normal, observed = (*truth, float(self.data.time)) if estimate is None else estimate
                rotation = self.data.xmat[self.env._pelvis_id].reshape(3, 3)
                point = rotation.T @ (position - self.data.xpos[self.env._pelvis_id])
                normal = rotation.T @ normal
                tracker = {"status": "tracking", "position_base": point, "surface_normal_base": normal,
                           "observed_at": observed}
                if self.arm_release is not None:  # a new press before the arm is back: the gait's gains first
                    self.model.actuator_gainprm[self.arm_release["actuators"]], \
                        self.model.actuator_biasprm[self.arm_release["actuators"]] = self.arm_release["gains"]
                self.arm_release, self.press_result = None, None
                self.press = PressController(self.env, point, tracker, planner=G1Planner())
                done = threading.Event()
                self.press_done = done
                plan, goal = self._arm_plan(self.press)
            except (SkillPreconditionError, ValueError) as error:
                goal_handle.abort()
                result.message = f"{getattr(error, 'code', 'refused')}: {error}"
                self.get_logger().warn(f"press {button} refused: {result.message}")
                return result

        self.arm_plan_pub.publish(plan)
        self.arm_goal_pub.publish(goal)
        self.get_logger().info(f"press {button}: target in pelvis frame {np.round(point, 3).tolist()}, "
                               f"plan {len(self.press.trajectory)} samples")
        if estimate is not None:
            angle = math.degrees(math.acos(float(np.clip(estimate[1] @ truth[1], -1.0, 1.0))))
            self.get_logger().info(f"press {button}: track_3d position {100 * np.linalg.norm(estimate[0] - truth[0]):.1f} "
                                   f"cm, normal {angle:.0f} deg from the sim truth")

        while not done.wait(timeout=0.1):
            if goal_handle.is_cancel_requested:
                with self.lock:
                    if self.press is not None:  # it can end in the same tick
                        self.press.close()
                        self.press = None
                goal_handle.canceled()
                result.message = "cancelled"
                return result

            feedback = VisualServoingArm.Feedback()
            with self.lock:
                values = dict(self.press.feedback) if self.press is not None else {}
            feedback.distance_to_goal = float(values.get("distance_to_goal", 0.0))
            feedback.orientation_error_deg = float(values.get("orientation_error_deg", 0.0))
            feedback.elapsed_time = float(values.get("elapsed_time", 0.0))
            goal_handle.publish_feedback(feedback)

        success, code, details = self.press_result
        result.success = bool(success)
        result.final_distance = float(details["final_distance"])
        result.orientation_error_deg = float(details["orientation_error_deg"])
        result.message = f"{code}: {details.get('reason', details['message'])}"
        (goal_handle.succeed if success else goal_handle.abort)()
        self.get_logger().info(f"press {button}: {result.message}, tip {result.final_distance:.3f} m from the goal")

        return result

    def track_button(
        self,
        goal_handle,
        count: int = 5,
        timeout: float = 10.0,  # Cosmos ~2 s, EdgeTAM start ~1 s, 5 poses at 5 Hz 1 s: a miss is known at once
    ) -> tuple[np.ndarray, np.ndarray, float] | None:
        """As the robot's skill: reset track_3d and send the query; then the median position of the first `count` poses
        of later images (world), their mean normal and the newest image time. None if they do not come in time."""
        self.button_poses, self.query_time = [], float(self.data.time)
        self.track_reset_pub.publish(Empty())
        self.query_pub.publish(String(data=goal_handle.request.target_description))
        end = time.monotonic() + timeout
        while len(self.button_poses) < count and time.monotonic() < end and not goal_handle.is_cancel_requested:
            time.sleep(0.1)
        poses = self.button_poses[:count]
        if len(poses) < count:
            return None
        normal = np.mean([normal for _, _, normal in poses], axis=0)

        return np.median([position for _, position, _ in poses], axis=0), normal / np.linalg.norm(normal), poses[-1][0]

    def _arm_plan(
        self,
        press: PressController,
    ) -> tuple[Path, PoseStamped]:
        """The G1Pilot joint path as the fingertip path in the world (FK every 50 ms; the pelvis is locked) and the goal."""
        scratch = mujoco.MjData(self.model)
        mujoco.mj_copyData(scratch, self.model, self.data)
        plan = Path()
        plan.header.stamp, plan.header.frame_id = self.stamp(), "world"
        for joints in press.trajectory[::5]:
            scratch.qpos[press.qpos] = joints
            mujoco.mj_kinematics(self.model, scratch)
            pose = PoseStamped(header=plan.header)
            pose.pose.position.x, pose.pose.position.y, pose.pose.position.z = map(float, scratch.site_xpos[press.site])
            x, y, z, w = matrix_quaternion_xyzw(scratch.site_xmat[press.site].reshape(3, 3))
            pose.pose.orientation.x, pose.pose.orientation.y, pose.pose.orientation.z, pose.pose.orientation.w = x, y, z, w
            plan.poses.append(pose)

        goal = PoseStamped(header=plan.header)
        goal.pose.position.x, goal.pose.position.y, goal.pose.position.z = map(float, press.goal_world)
        rotation = self.data.xmat[self.env._pelvis_id].reshape(3, 3) @ press.goal[:3, :3]  # +x into the panel
        x, y, z, w = matrix_quaternion_xyzw(rotation)
        goal.pose.orientation.x, goal.pose.orientation.y, goal.pose.orientation.z, goal.pose.orientation.w = x, y, z, w

        return plan, goal

    def _update_press(self) -> None:
        """The arm controller runs inside env.step; here its end is noticed: a stop (collision, base motion), or the end
        of the press hold. Then the legs are free at once, and the arm holds at the button until release_arms."""
        press = self.press
        if press is None or not (press.done or press.phase == "withdraw"):
            return
        if not press.done:
            self.arm_release = {"path": press.trajectory, "dt": press.dt, "actuators": press.actuators,
                                "clock": max(0.0, 2 * press.duration + 0.6 - (self.data.time - press.started)),
                                "correction": press.correction.copy(), "since": None,
                                "gains": (press.saved_gain, press.saved_bias), "qpos": press.qpos, "dofs": press.dofs,
                                "site": press.site, "car": press.car}
            press.done = True
        firm = (self.model.actuator_gainprm[press.actuators].copy(), self.model.actuator_biasprm[press.actuators].copy())
        self.press_result = press.result()
        press.close()
        if self.arm_release is not None:
            # Keep the press servo gains until the arm is back, as the arm SDK holds the arm: with the gait's gains the
            # stretched arm sagged 15 cm down the panel in 0.2 s and pressed the button below (run 26)
            self.model.actuator_gainprm[press.actuators], self.model.actuator_biasprm[press.actuators] = firm
        self.press = None
        self.press_done.set()

    def shutdown(self) -> None:
        self.running = False
        self.thread.join(timeout=30)


def main() -> None:
    """Spin the node until Ctrl-C; the physics loop runs on its own thread."""
    rclpy.init()
    node = SimOrin()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)

    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
