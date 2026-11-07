"""Video of one FVD sim run from its MCAP bag: a landing camera, a car camera, the top view of what the robot believes
(prior map, lidar scan, global and local plans, relocalization) and a G1 close-up with the arm plan.

python3 render_run.py <bag.mcap> <places.yaml> <map.pcd> <output.mp4> [--preview t1,t2,...]

The 3D views replay /sim/replay (the full sim state, 15 Hz) in elevator_sim's MuJoCo scene, so run it with the sim's
Python (PYTHONPATH with elevator_sim, see config/vector_pc.env); the bag is read with mcap_ros2 (no ROS needed).
--preview writes PNG frames at those times [s from the command] instead of the video.
"""
import argparse
import bisect
import json
import math
import shutil
import subprocess

import mujoco
import numpy as np
import yaml

from mcap_ros2.reader import read_ros2_messages
from PIL import Image, ImageDraw, ImageFont

from elevator_sim import ElevatorEnvironment
from live_route import live_routes, load_planner

FPS = 15
PANE_W, PANE_H = 960, 540
SCALE = 110.0  # top view [px/m]
TOPICS = ["/sim/replay", "/sim/arm_plan", "/sim/arm_goal", "/task_executor/status", "/task_executor/goal", "/task_executor/plan",
          "/route_planner/path", "/path", "/free_paths", "/way_point", "/registered_scan", "/odom_base_link", "/sim/ground_truth",
          "/initialpose", "/pf_relocalize/particles", "/pf_relocalize/status", "/slam_elevatorlio/status",
          "/camera/camera/color/camera_info", "/keyboard_input", "/init_bbox", "/track_3d/detections_2d",
          "/track_3d/selected_target_pointcloud", "/track_3d/selected_normal_pose", "/sim/button_pose", "/route_goal"]
# Executor status details: a planner walk (route planner -> local planner), or one of the executor's own moves
PLANNER_WALK = "walking:"
OWN_MOVES = ("stepping back:", "turning:", "walking straight:", "aligning:")
# Landing camera tripods per floor (x, y, z in the world): the camera pans and zooms to keep the robot in the frame
TRIPODS = {0.0: (4.6, 0.0, 2.3), 3.8: (-0.8, -4.5, 5.9)}
# Landing door cameras per floor (position, look at), used while the robot is in the doorway or in the car
DOOR_CAMERAS = {0.0: ((1.3, -3.3, 2.1), (0.0, 0.4, 1.1)), 3.8: ((-0.8, -4.5, 5.9), (0.0, 0.4, 4.9))}
# Car camera in the car frame: in the back right corner, on the panel, the doors and the robot
CAR_CAMERA = ((1.15, 2.45, 2.25), (-0.75, 0.55, 1.0), 62.0)
COLOURS = {"global": (70, 140, 255), "local": (230, 60, 220), "free": (105, 55, 110), "scan": (255, 150, 30), "goal": (255, 200, 0),
           "guess": (255, 235, 60), "particles": (60, 230, 230), "truth": (90, 220, 110), "robot": (240, 240, 240),
           "place": (150, 160, 170), "plan": (60, 230, 230), "tip": (90, 220, 110), "own": (255, 80, 80),
           "seed": (255, 235, 60), "mask": (0, 255, 150), "seen": (255, 140, 0)}


# ========== Bag ==========

class Bag:
    """Messages per topic in time order [s, MCAP log time], and lookups by time."""

    def __init__(
        self,
        path: str,
    ) -> None:
        self.times, self.messages = {topic: [] for topic in TOPICS}, {topic: [] for topic in TOPICS}
        for message in read_ros2_messages(path, topics=TOPICS):
            topic, value = message.channel.topic, message.ros_msg
            if topic in ("/registered_scan", "/free_paths"):
                value = cloud_xyz(value)
            elif topic == "/track_3d/selected_target_pointcloud":
                value = cloud_xyz(value, keep=50000)  # every mask pixel with depth
            elif topic in ("/sim/replay", "/task_executor/status", "/pf_relocalize/status", "/slam_elevatorlio/status"):
                value = json.loads(value.data)
            self.times[topic].append(message.log_time_ns * 1e-9)
            self.messages[topic].append(value)

    def add_live_routes(
        self,
        map_pcd: str,
    ) -> None:
        """Replace ~/path (the stock route planner sends it once per goal) by its route from the robot's cell at each
        odometry message where that cell changed, rebuilt with the route planner's own code (live_route.py): x, y arrays."""
        goal_topic = "/route_goal" if self.times["/route_goal"] else "/task_executor/goal"  # no /route_goal before run 34
        goals = list(zip(self.times[goal_topic], self.messages[goal_topic]))
        odometry = list(zip(self.times["/odom_base_link"], self.messages["/odom_base_link"]))
        routes = live_routes(load_planner(map_pcd), goals, odometry, self.times["/odom_base_link"])
        self.times["/route_planner/path"] = [t for t, _ in routes]
        self.messages["/route_planner/path"] = [route[:, :2] for _, route in routes]

    def last(
        self,
        topic: str,
        t: float,
        age: float = math.inf,
    ) -> tuple[float, object] | None:
        """The last message at or before t, if not older than age [s]."""
        index = bisect.bisect_right(self.times[topic], t) - 1
        if index < 0 or t - self.times[topic][index] > age:
            return None

        return self.times[topic][index], self.messages[topic][index]

    def between(
        self,
        topic: str,
        start: float,
        end: float,
    ) -> list[tuple[float, object]]:
        first, last = bisect.bisect_right(self.times[topic], start), bisect.bisect_right(self.times[topic], end)

        return list(zip(self.times[topic][first:last], self.messages[topic][first:last]))


def cloud_xyz(
    cloud,
    keep: int = 2500,
) -> np.ndarray:
    """x, y, z float32 of a PointCloud2 with float32 x, y, z fields, every n-th point down to ~keep points."""
    offsets = {field.name: field.offset for field in cloud.fields}
    rows = np.frombuffer(bytes(cloud.data), dtype=np.uint8).reshape(-1, cloud.point_step)
    rows = rows[:: max(1, len(rows) // keep)]

    return np.stack([rows[:, offsets[name]:offsets[name] + 4].copy().view(np.float32)[:, 0] for name in "xyz"], 1)


def pose_xyyaw(
    pose,
) -> np.ndarray:
    """x, y, z, yaw of a geometry_msgs Pose."""
    q = pose.orientation

    return np.array([pose.position.x, pose.position.y, pose.position.z,
                     math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))])


def quaternion_matrix(
    q,
) -> np.ndarray:
    x, y, z, w = q.x, q.y, q.z, q.w

    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


# ========== 3D views ==========

class Scene:
    """elevator_sim's MuJoCo scene set to the recorded state; free cameras with markers drawn into the scene."""

    def __init__(self) -> None:
        self.env = ElevatorEnvironment(robot_motion="unitree_rl")
        self.model, self.data = self.env.model, self.env.data
        self.renderer = mujoco.Renderer(self.model, PANE_H, PANE_W)
        self.head = mujoco.Renderer(self.model, 360, 640)
        self.option = mujoco.MjvOption()
        self.option.geomgroup[3] = self.option.geomgroup[4] = 0
        self.rings = [spec["ring"] for spec in self.env._button_registry.values() if spec["ring"] >= 0]
        self.tip = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, "right_dex1_tip")
        self.car = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "elevator_car")
        self.pelvis = self.env._pelvis_id

    def set_state(
        self,
        replay: dict,
    ) -> None:
        """qpos, qvel and the lit button rings of one /sim/replay message."""
        self.data.qpos[:] = replay["qpos"]
        self.data.qvel[:] = replay["qvel"]
        for ring in self.rings:
            self.model.geom_matid[ring] = self.env._ring_lit if ring in replay["lit"] else self.env._ring_dark
        mujoco.mj_forward(self.model, self.data)
        self.env._update_floor_display()

    def render(
        self,
        position: np.ndarray,
        lookat: np.ndarray,
        fovy: float,
        markers: list,
    ) -> np.ndarray:
        """A free camera at position looking at lookat; markers are (kind, size, a, b or rotation, rgba)."""
        offset = np.asarray(lookat) - np.asarray(position)
        camera = mujoco.MjvCamera()
        camera.type = mujoco.mjtCamera.mjCAMERA_FREE
        camera.lookat[:] = lookat
        camera.distance = float(np.linalg.norm(offset))
        camera.azimuth = math.degrees(math.atan2(offset[1], offset[0]))
        camera.elevation = math.degrees(math.atan2(offset[2], math.hypot(offset[0], offset[1])))
        self.model.vis.global_.fovy = fovy
        self.renderer.update_scene(self.data, camera=camera, scene_option=self.option)
        scene = self.renderer.scene
        for kind, size, a, b, rgba in markers:
            if scene.ngeom >= scene.maxgeom:
                break
            geom = scene.geoms[scene.ngeom]
            rgba = np.array([*rgba[:3], 255 if len(rgba) < 4 else rgba[3]], dtype=np.float32) / 255.0
            if kind == "segment":
                mujoco.mjv_initGeom(geom, mujoco.mjtGeom.mjGEOM_CAPSULE, np.zeros(3), np.zeros(3), np.eye(3).ravel(), rgba)
                mujoco.mjv_connector(geom, mujoco.mjtGeom.mjGEOM_CAPSULE, size, np.asarray(a, float), np.asarray(b, float))
            else:
                mujoco.mjv_initGeom(geom, kind, np.asarray(size, float), np.asarray(a, float), np.asarray(b, float).ravel(), rgba)
            scene.ngeom += 1

        return self.renderer.render().copy()

    def head_view(self) -> np.ndarray:
        """The robot's head camera as it streams (640x480, the centre 4:3 crop of the 16:9 colour sensor)."""
        self.head.update_scene(self.data, camera="g1_head")

        return self.head.render()[:, 80:560].copy()


def sphere(
    position: np.ndarray,
    radius: float,
    rgba: tuple,
) -> tuple:
    return mujoco.mjtGeom.mjGEOM_SPHERE, (radius, radius, radius), position, np.eye(3), rgba


def arrow(
    start: np.ndarray,
    direction: np.ndarray,
    length: float,
    width: float,
    rgba: tuple,
) -> tuple:
    """An arrow from start along direction (its geom z axis)."""
    z = np.asarray(direction, float) / np.linalg.norm(direction)
    x = np.cross([0.0, 0.0, 1.0], z) if abs(z[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    x /= np.linalg.norm(x)

    return mujoco.mjtGeom.mjGEOM_ARROW, (width, width, length), start, np.stack([x, np.cross(z, x), z], 1), rgba


def path_markers(
    points: np.ndarray,
    radius: float,
    rgba: tuple,
    dots: int = 0,
) -> list:
    """Capsules along a polyline; a sphere at every dots-th point."""
    markers = [("segment", radius, a, b, rgba) for a, b in zip(points[:-1], points[1:])]
    if dots:
        markers += [sphere(point, 2.2 * radius, rgba) for point in points[::dots]]

    return markers


# ========== Top view ==========

class TopView:
    """The robot's belief from above, centred on it: prior map of its floor, scan, plans, goal, relocalization."""

    def __init__(
        self,
        points: np.ndarray,
        places: dict,
    ) -> None:
        self.places, self.floors = places, places["floors"]
        self.origin = points[:, :2].min(axis=0) - 6.0
        size = np.ceil((points[:, :2].max(axis=0) + 6.0 - self.origin) * SCALE).astype(int)
        self.height = size[1]
        self.rasters = {}
        for name, floor in self.floors.items():
            raster = np.zeros((size[1], size[0], 3), dtype=np.uint8)
            raster[:] = (14, 18, 24)
            height = points[:, 2] - floor["ground_z"]
            for band, colour in (((-0.15, 0.15), (38, 46, 56)), ((0.3, 2.0), (205, 210, 216))):
                rows, cols = self.pixels(points[(height > band[0]) & (height < band[1]), :2])
                for dr, dc in ((0, 0), (1, 0), (0, 1), (1, 1)):
                    raster[np.clip(rows + dr, 0, size[1] - 1), np.clip(cols + dc, 0, size[0] - 1)] = colour
            self.rasters[name] = raster
        self.font = ImageFont.load_default(size=15)
        self.small = ImageFont.load_default(size=12)
        self.centre = None

    def pixels(
        self,
        xy: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Raster rows, columns of map x, y (y up)."""
        cols = ((xy[:, 0] - self.origin[0]) * SCALE).astype(int)
        rows = (self.height - 1 - (xy[:, 1] - self.origin[1]) * SCALE).astype(int)

        return rows, cols

    def floor_of(
        self,
        z: float,
    ) -> str:
        """The floor whose base_link height is nearest to z."""
        return min(self.floors, key=lambda name: abs(self.floors[name]["ground_z"] + self.places["base_height"] - z))

    def draw(
        self,
        t: float,
        bag: Bag,
        fresh: bool,
        arm_plan: np.ndarray | None,
    ) -> np.ndarray:
        """The view at t; fresh: no smoothing from the last frame; arm_plan: the planned fingertip path while active."""
        odom = bag.last("/odom_base_link", t)
        estimate = pose_xyyaw(odom[1].pose.pose) if odom else np.zeros(4)
        floor = self.floor_of(estimate[2])
        target = estimate[:2]
        self.centre = target if fresh else self.centre + 0.2 * (target - self.centre)

        # Crop the floor's raster around the robot; to_px maps map x, y to window pixels
        raster = self.rasters[floor]
        rows, cols = self.pixels(self.centre[None])
        top, left = int(rows[0]) - PANE_H // 2, int(cols[0]) - PANE_W // 2
        image = Image.fromarray(raster[top:top + PANE_H, left:left + PANE_W].copy())
        draw = ImageDraw.Draw(image)

        def to_px(xy: np.ndarray) -> np.ndarray:
            r, c = self.pixels(np.atleast_2d(xy))
            return np.stack([c - left, r - top], 1)

        def draw_arrow(pose: np.ndarray, colour: tuple, length: float = 0.35, fill: bool = True, width: int = 2) -> None:
            tip = pose[:2] + length * np.array([math.cos(pose[-1]), math.sin(pose[-1])])
            side = 0.4 * length * np.array([-math.sin(pose[-1]), math.cos(pose[-1])])
            corners = to_px(np.array([tip, pose[:2] + side - 0.3 * (tip - pose[:2]), pose[:2] - side - 0.3 * (tip - pose[:2])]))
            draw.polygon([tuple(p) for p in corners], fill=colour if fill else None, outline=colour, width=width)

        # Annotated places of this floor and the car
        poses = {**{n: p for n, p in self.floors[floor].items() if isinstance(p, dict) and "yaw" in p},
                 **{f"car {n}": p for n, p in self.places["car"].items() if isinstance(p, dict)},
                 **self.floors[floor]["targets"]}
        for name, pose in poses.items():
            draw_arrow(np.array([pose["x"], pose["y"], pose["yaw"]]), COLOURS["place"], 0.22, fill=False, width=1)
            x, y = to_px(np.array([pose["x"], pose["y"]]))[0]
            draw.text((x + 6, y + 4), name, font=self.small, fill=COLOURS["place"])

        # Lidar scan (registered in the map frame by LIO + the map match): obstacles above the floor
        scan = bag.last("/registered_scan", t, 0.25)
        if scan is not None:
            points = scan[1]
            height = points[:, 2] - self.floors[floor]["ground_z"]
            pixels = to_px(points[(height > 0.15) & (height < 2.0), :2])
            inside = (pixels[:, 0] >= 0) & (pixels[:, 0] < PANE_W - 1) & (pixels[:, 1] >= 0) & (pixels[:, 1] < PANE_H - 1)
            array = np.asarray(image).copy()
            for dr, dc in ((0, 0), (1, 0), (0, 1), (1, 1)):
                array[pixels[inside, 1] + dr, pixels[inside, 0] + dc] = COLOURS["scan"]
            image = Image.fromarray(array)
            draw = ImageDraw.Draw(image)

        # Global plan (the route planner's route from the robot's cell, rebuilt: Bag.add_live_routes) and its carrot (2 m
        # ahead on that route, 5 Hz): the local planner steers at the carrot. Only while the planners walk
        status = bag.last("/task_executor/status", t)
        detail = status[1].get("detail", "") if status else ""
        goal = bag.last("/task_executor/goal", t)
        route = bag.last("/route_planner/path", t)
        planner_walk = detail.startswith(PLANNER_WALK)
        if route and goal and route[0] > goal[0] - 1.0 and planner_walk:
            poses = route[1]
            if len(poses) > 1:
                draw.line([tuple(p) for p in to_px(poses)], fill=COLOURS["global"], width=3)
                for p in to_px(poses):
                    draw.ellipse((p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3), fill=COLOURS["global"])
            waypoint = bag.last("/way_point", t, 2.0)
            if waypoint:
                p = to_px(np.array([waypoint[1].point.x, waypoint[1].point.y]))[0]
                draw.line([tuple(to_px(estimate[:2])[0]), tuple(p)], fill=COLOURS["global"], width=1)
                draw.rectangle((p[0] - 6, p[1] - 6, p[0] + 6, p[1] + 6), outline=COLOURS["global"], width=3)

        # Local planner's collision-free candidate paths (base_link)
        c, s = math.cos(estimate[3]), math.sin(estimate[3])
        free = bag.last("/free_paths", t, 0.4)
        if free is not None and odom and planner_walk:
            for x, y in to_px(estimate[:2] + free[1][:, :2] @ np.array([[c, s], [-s, c]])):
                draw.point((x, y), fill=COLOURS["free"])

        # The executor's own move (step back, turn, straight walk, final approach): its straight line
        plan = bag.last("/task_executor/plan", t)
        if plan and detail.startswith(OWN_MOVES) and len(plan[1].poses) > 1:
            points = to_px(np.array([[p.pose.position.x, p.pose.position.y] for p in plan[1].poses]))
            draw.line([tuple(p) for p in points], fill=COLOURS["own"], width=3)
            draw.ellipse((points[-1][0] - 5, points[-1][1] - 5, points[-1][0] + 5, points[-1][1] + 5), outline=COLOURS["own"], width=2)

        # Arm motion plan (G1Pilot fingertip path) from above
        if arm_plan is not None:
            draw.line([tuple(p) for p in to_px(arm_plan[:, :2])], fill=COLOURS["plan"], width=3)

        # The executor's goal (route goal or final approach)
        if goal:
            draw_arrow(pose_xyyaw(goal[1].pose)[[0, 1, 3]], COLOURS["goal"], 0.3, fill=False, width=3)

        # Relocalization: the guesses sent to GICP, the particle filter's particles and result
        for stamp, message in bag.between("/initialpose", t - 6.0, t):
            guess = pose_xyyaw(message.pose.pose)
            draw_arrow(guess[[0, 1, 3]], COLOURS["guess"], 0.45, fill=False, width=3)
            x, y = to_px(guess[:2])[0]
            draw.text((x + 10, y - 18), "guess -> GICP", font=self.font, fill=COLOURS["guess"])
        particles = bag.last("/pf_relocalize/particles", t, 1.5)
        if particles:
            for p in particles[1].poses:
                x, y = to_px(np.array([p.position.x, p.position.y]))[0]
                draw.point([(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)], fill=COLOURS["particles"])

        # The robot: estimate (/odom_base_link, filled) and the sim truth (pelvis, outline)
        truth = bag.last("/sim/ground_truth", t)
        if truth:
            draw_arrow(pose_xyyaw(truth[1].pose.pose)[[0, 1, 3]], COLOURS["truth"], 0.4, fill=False, width=2)
        if odom:
            draw_arrow(estimate[[0, 1, 3]], COLOURS["robot"], 0.4)

        # Local planner's chosen path (/path, base_link, ~0.7 m from the robot): on top of the robot, or it hides under it
        local = bag.last("/path", t, 0.4)
        if local and odom and len(local[1].poses) > 1 and planner_walk:
            xy = np.array([[p.pose.position.x, p.pose.position.y] for p in local[1].poses])
            world = to_px(estimate[:2] + xy @ np.array([[c, s], [-s, c]]))
            draw.line([tuple(p) for p in world], fill=COLOURS["local"], width=4)
            draw.ellipse((world[-1][0] - 4, world[-1][1] - 4, world[-1][0] + 4, world[-1][1] + 4), fill=COLOURS["local"])

        # Text: map floor, localization, particle filter
        slam = bag.last("/slam_elevatorlio/status", t)
        lines = [f"prior map: floor {floor} (from the estimated height)"]
        if slam:
            s = slam[1]
            state = "localized" if s.get("localized") else "NOT localized"
            lines.append(f"GICP to the map: {state}, structure inliers {s.get('structure_inlier_ratio', 0):.2f}")
            if s.get("error"):
                lines.append(s["error"][:90])
        pf = bag.last("/pf_relocalize/status", t, 8.0)
        if pf:
            lines.append(f"particle filter: {pf[1].get('state')} ({pf[1].get('x')}, {pf[1].get('y')}, "
                         f"{pf[1].get('yaw_deg')} deg), fit {pf[1].get('fit_m')} m")
        draw.rectangle((0, PANE_H - 18 * len(lines) - 10, 560, PANE_H), fill=(14, 18, 24))
        for index, line in enumerate(lines):
            draw.text((8, PANE_H - 18 * (len(lines) - index) - 4), line, font=self.font, fill=(230, 236, 240))
        legend = [("robot estimate", COLOURS["robot"]), ("sim truth", COLOURS["truth"]), ("lidar scan", COLOURS["scan"]),
                  ("global plan, carrot", COLOURS["global"]), ("local plan", COLOURS["local"]), ("local free paths", COLOURS["free"]),
                  ("executor's own move", COLOURS["own"]), ("arm plan", COLOURS["plan"]), ("goal", COLOURS["goal"]),
                  ("guess", COLOURS["guess"]), ("particles", COLOURS["particles"])]
        draw.rectangle((0, 70, 150, 78 + 18 * len(legend)), fill=(14, 18, 24))
        for index, (name, colour) in enumerate(legend):
            draw.rectangle((8, 76 + 18 * index, 20, 88 + 18 * index), fill=colour)
            draw.text((26, 74 + 18 * index), name, font=self.small, fill=(230, 236, 240))
        draw.line([(PANE_W - 40 - int(SCALE), 60), (PANE_W - 40, 60)], fill=(230, 236, 240), width=3)
        draw.text((PANE_W - 40 - int(SCALE), 40), "1 m", font=self.small, fill=(230, 236, 240))

        return np.asarray(image)


# ========== Video ==========

class Director:
    """Per frame: the four panes, the markers and the text."""

    def __init__(
        self,
        bag: Bag,
        places: dict,
        points: np.ndarray,
    ) -> None:
        self.bag, self.places = bag, places
        self.scene = Scene()
        self.top = TopView(points, places)
        self.font = ImageFont.load_default(size=22)
        self.label = ImageFont.load_default(size=17)
        self.follow = None   # smoothed x, y, yaw, floor z of the robot
        self.look = None     # smoothed landing camera target
        self.last_t = None   # time of the last frame: the smoothing restarts after a gap
        self.trace, self.plan_time, self.arm_plan = [], None, None
        self.corners = np.array(places["car"]["corners"], dtype=float)

    def robot(self) -> tuple[np.ndarray, float, float]:
        """Pelvis x, y, z, its yaw and the floor z under it (the car floor in the car)."""
        data, scene = self.scene.data, self.scene
        rotation = data.xmat[scene.pelvis].reshape(3, 3)
        position = data.xpos[scene.pelvis].copy()

        return position, math.atan2(rotation[1, 0], rotation[0, 0]), position[2] - 0.79

    def in_car(
        self,
        position: np.ndarray,
    ) -> bool:
        (x0, y0), (x1, y1) = self.corners.min(axis=0), self.corners.max(axis=0)
        return x0 < position[0] < x1 and y0 < position[1] < y1

    def markers(
        self,
        t: float,
        tip: np.ndarray,
    ) -> list:
        """Arm plan (planned fingertip path, goal), the executed fingertip path, and the executor's goal on the floor."""
        markers, self.arm_plan = [], None
        plan = self.bag.last("/sim/arm_plan", t, 60.0)
        arm_done = [stamp for stamp, status in self.bag.between("/task_executor/status", plan[0], t)
                    if status.get("detail", "").startswith("arm:")] if plan else []
        if plan and (not arm_done or t - arm_done[0] < 3.0):
            if plan[0] != self.plan_time:
                self.plan_time, self.trace = plan[0], []
            self.trace.append(tip.copy())
            points = np.array([[p.pose.position.x, p.pose.position.y, p.pose.position.z] for p in plan[1].poses])
            markers += path_markers(points, 0.0035, (*COLOURS["plan"], 200), dots=4)
            self.arm_plan = points
            goal = self.bag.last("/sim/arm_goal", t, 60.0)
            if goal:
                position = np.array([goal[1].pose.position.x, goal[1].pose.position.y, goal[1].pose.position.z])
                axis = quaternion_matrix(goal[1].pose.orientation)[:, 0]
                markers += [sphere(position, 0.012, (255, 70, 70, 220)),
                            arrow(position - 0.1 * axis, axis, 0.08, 0.006, (255, 70, 70, 220))]
            if len(self.trace) > 1:
                markers += path_markers(np.array(self.trace), 0.004, (*COLOURS["tip"], 255))

        goal = self.bag.last("/task_executor/goal", t)
        status = self.bag.last("/task_executor/status", t)
        if goal and status and status[1].get("state") == "running":
            pose = pose_xyyaw(goal[1].pose)
            floor = self.robot()[2]
            start = np.array([pose[0], pose[1], floor + 0.03])
            direction = np.array([math.cos(pose[3]), math.sin(pose[3]), 0.0])
            markers.append(arrow(start - 0.15 * direction, direction, 0.3, 0.02, (*COLOURS["goal"], 170)))

        # Walk plans on the floor: global and local path while the planners walk, the executor's own straight move
        detail, floor = status[1].get("detail", "") if status else "", self.robot()[2]
        if detail.startswith(PLANNER_WALK):
            route = self.bag.last("/route_planner/path", t)
            if route and goal and route[0] > goal[0] - 1.0 and len(route[1]) > 1:
                points = np.c_[route[1], np.full(len(route[1]), floor + 0.02)]
                markers += path_markers(points, 0.01, (*COLOURS["global"], 220), dots=1)
            local, odom = self.bag.last("/path", t, 0.4), self.bag.last("/odom_base_link", t)
            if local and odom and len(local[1].poses) > 1:
                estimate = pose_xyyaw(odom[1].pose.pose)
                c, s = math.cos(estimate[3]), math.sin(estimate[3])
                xy = estimate[:2] + np.array([[p.pose.position.x, p.pose.position.y] for p in local[1].poses]) @ np.array([[c, s], [-s, c]])
                markers += path_markers(np.c_[xy, np.full(len(xy), floor + 0.03)], 0.014, (*COLOURS["local"], 255))
        plan = self.bag.last("/task_executor/plan", t)
        if detail.startswith(OWN_MOVES) and plan and len(plan[1].poses) > 1:
            points = np.array([[p.pose.position.x, p.pose.position.y, floor + 0.02] for p in plan[1].poses])
            markers += path_markers(points, 0.012, (*COLOURS["own"], 230), dots=1)

        # The button pose from Thor's track_3d (sim_orin puts it in the world), while a press uses it
        seen = self.bag.last("/sim/button_pose", t, 1.0)
        if seen and self.perceiving(t):
            position = np.array([seen[1].pose.position.x, seen[1].pose.position.y, seen[1].pose.position.z])
            normal = quaternion_matrix(seen[1].pose.orientation)[:, 0]
            markers += [sphere(position, 0.011, (*COLOURS["seen"], 240)), arrow(position, normal, 0.14, 0.007, (*COLOURS["seen"], 240))]

        return markers

    def perceiving(
        self,
        t: float,
    ) -> bool:
        """From a press query on /keyboard_input to 3 s after the press result."""
        query = self.bag.last("/keyboard_input", t, 90.0)
        if not query:
            return False
        done = [stamp for stamp, status in self.bag.between("/task_executor/status", query[0], t)
                if status.get("detail", "").startswith("arm:")]

        return not done or t - done[0] < 3.0

    def head_overlay(
        self,
        t: float,
        head: np.ndarray,
    ) -> Image.Image:
        """The head view with Thor's perception on it: the Cosmos seed box (yellow), the EdgeTAM mask (its pixels with
        depth, from track_3d's segmented cloud) and box (green), the track_3d pose (orange: centre, 6 cm of the normal)."""
        image = np.asarray(head).copy()
        info = self.bag.last("/camera/camera/color/camera_info", t)
        if info is None:
            return Image.fromarray(image)
        fx, _, cx, _, fy, cy = info[1].k[:6]
        scale = image.shape[1] / info[1].width  # the 640x480 stream -> this view, same field of view

        def to_px(points: np.ndarray) -> np.ndarray:
            return np.c_[fx * points[:, 0] / points[:, 2] + cx, fy * points[:, 1] / points[:, 2] + cy] * scale

        cloud = self.bag.last("/track_3d/selected_target_pointcloud", t, 0.6)
        if cloud and len(cloud[1]):
            u, v = np.round(to_px(cloud[1].astype(float))).astype(int).T
            inside = (u >= 0) & (u < image.shape[1]) & (v >= 0) & (v < image.shape[0])
            image[v[inside], u[inside]] = 0.45 * image[v[inside], u[inside]] + 0.55 * np.array(COLOURS["mask"])
        view = Image.fromarray(image)
        draw = ImageDraw.Draw(view)
        for topic, colour, age in (("/init_bbox", COLOURS["seed"], 4.0), ("/track_3d/detections_2d", COLOURS["mask"], 0.6)):
            found = self.bag.last(topic, t, age)
            for detection in found[1].detections if found else []:
                box = detection.bbox
                u0, v0 = (box.center.position.x - box.size_x / 2) * scale, (box.center.position.y - box.size_y / 2) * scale
                draw.rectangle((u0, v0, u0 + box.size_x * scale, v0 + box.size_y * scale), outline=colour, width=2)
        pose = self.bag.last("/track_3d/selected_normal_pose", t, 0.6)
        if pose:
            origin = np.array([pose[1].pose.position.x, pose[1].pose.position.y, pose[1].pose.position.z])
            (u0, v0), (u1, v1) = to_px(np.array([origin, origin + 0.06 * quaternion_matrix(pose[1].pose.orientation)[:, 0]]))
            draw.line((u0, v0, u1, v1), fill=COLOURS["seen"], width=3)
            draw.ellipse((u0 - 4, v0 - 4, u0 + 4, v0 + 4), fill=COLOURS["seen"])

        return view

    def frame(
        self,
        t: float,
        t0: float,
    ) -> Image.Image:
        fresh = self.last_t is None or abs(t - self.last_t) > 0.5
        self.last_t = t
        replay = self.bag.last("/sim/replay", t)
        self.scene.set_state(replay[1])
        position, yaw, floor = self.robot()
        tip = self.scene.data.site_xpos[self.scene.tip].copy()
        markers = self.markers(t, tip)
        torso = position + np.array([0.0, 0.0, 0.35])

        # A: landing tripod of the robot's floor, panning and zooming on the robot; the door camera in the doorway and car
        level = min(TRIPODS, key=lambda z: abs(z - floor))
        doorway = abs(position[0]) < 0.5 and -1.6 < position[1] < 0.5
        self.look = torso if fresh else self.look + 0.25 * (torso - self.look)
        if doorway or self.in_car(position):
            landing = self.scene.render(np.array(DOOR_CAMERAS[level][0]), np.array(DOOR_CAMERAS[level][1]), 50.0, markers)
        else:
            tripod = np.array(TRIPODS[level])
            distance = float(np.linalg.norm(self.look - tripod))
            landing = self.scene.render(tripod, self.look, float(np.clip(math.degrees(2 * math.atan(1.3 / distance)), 24, 60)), markers)

        # B: in the car, fixed to it
        car = self.scene.data.xpos[self.scene.car]
        (camera, lookat, fovy) = CAR_CAMERA
        cabin = self.scene.render(car + np.array(camera), car + np.array(lookat), fovy, markers)

        # D: close-up. In the car: fixed to the car, on the panel and the door front. In the doorway: behind the robot
        # (its side is in the wall there). Else it follows the robot at its right side, where the right arm works
        state = np.array([*position[:2], yaw, floor])
        if fresh:
            self.follow = state
        else:
            self.follow[:2] += 0.15 * (state[:2] - self.follow[:2])
            self.follow[2] += 0.1 * math.atan2(math.sin(yaw - self.follow[2]), math.cos(yaw - self.follow[2]))
            self.follow[3] += 0.3 * (floor - self.follow[3])
        x, y, heading, ground = self.follow
        forward, left = np.array([math.cos(heading), math.sin(heading)]), np.array([-math.sin(heading), math.cos(heading)])
        if self.in_car(position):
            close = self.scene.render(car + np.array([-0.15, 2.35, 1.75]), car + np.array([-0.45, 0.8, 1.0]), 55.0, markers)
        else:
            eye, aim = (-1.3 * forward + 0.25 * left, 0.6 * forward) if doorway else (0.3 * forward - 1.05 * left, 0.35 * forward - 0.12 * left)
            close = self.scene.render(np.array([x + eye[0], y + eye[1], ground + (1.6 if doorway else 1.5)]),
                                      np.array([x + aim[0], y + aim[1], ground + 1.05]), 50.0, markers)
        head = self.scene.head_view()

        # C: top view
        top = self.top.draw(t, self.bag, fresh, self.arm_plan)

        canvas = Image.new("RGB", (2 * PANE_W, 2 * PANE_H))
        for pane, (cx, cy) in ((landing, (0, 0)), (top, (PANE_W, 0)), (cabin, (0, PANE_H)), (close, (PANE_W, PANE_H))):
            canvas.paste(Image.fromarray(pane), (cx, cy))
        # During a press the head camera is larger, with Thor's perception on it
        if self.perceiving(t):
            canvas.paste(self.head_overlay(t, head), (2, PANE_H - 362))
            inset = ((2, PANE_H - 386), "G1 head camera: Cosmos box (yellow), EdgeTAM mask (green), track_3d pose (orange)")
        else:
            canvas.paste(Image.fromarray(head).resize((320, 240)), (2, PANE_H - 242))
            inset = ((2, PANE_H - 266), "G1 head camera, 640x480")
        draw = ImageDraw.Draw(canvas)
        labels = (((0, 0), f"A  landing camera, floor {'1' if level == 0.0 else '2'}"),
                  ((PANE_W, 0), "C  top view: what the robot believes"),
                  ((0, PANE_H), "B  car camera"),
                  ((PANE_W, PANE_H), "D  G1 close-up: arm plan (cyan), fingertip (green), track_3d button pose (orange), walk plans"),
                  inset)
        for (x0, y0), text in labels:
            y0 += 44 if y0 == 0 else 0
            draw.rectangle((x0, y0, x0 + 10 + draw.textlength(text, font=self.label), y0 + 24), fill=(15, 26, 34))
            draw.text((x0 + 5, y0 + 2), text, font=self.label, fill=(229, 237, 242))
        status = self.bag.last("/task_executor/status", t)
        step = f"{status[1].get('step', '')}: {status[1].get('state', '')}" if status else ""
        detail = status[1].get("detail", "") if status else ""
        draw.rectangle((0, 0, 2 * PANE_W, 42), fill=(15, 26, 34))
        draw.text((12, 8), f"t = {t - t0:5.1f} s   {step}", font=self.font, fill=(255, 209, 102))
        draw.text((560, 10), detail[:120], font=self.label, fill=(229, 237, 242))
        for x0 in (PANE_W,):
            draw.line((x0, 42, x0, 2 * PANE_H), fill=(15, 26, 34), width=3)
        draw.line((0, PANE_H, 2 * PANE_W, PANE_H), fill=(15, 26, 34), width=3)

        return canvas


def task_window(
    bag: Bag,
) -> tuple[float, float]:
    """From 2 s before the command is taken to 5 s after done or failed."""
    times = bag.times["/task_executor/status"]
    states = [status.get("state") for status in bag.messages["/task_executor/status"]]
    start = next(t for t, state in zip(times, states) if state == "running")
    end = next((t for t, state in zip(times, states) if t > start and state in ("done", "failed")), times[-1])

    return start - 2.0, end + 5.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bag")
    parser.add_argument("places")
    parser.add_argument("map")
    parser.add_argument("output")
    parser.add_argument("--preview", default="", help="t1,t2,... [s from the command]: PNG frames instead of the video")
    args = parser.parse_args()

    from slam_elevatorlio.map_io import read_pcd_xyz
    with open(args.places) as file:
        places = yaml.safe_load(file)
    bag = Bag(args.bag)
    bag.add_live_routes(args.map)
    start, end = task_window(bag)
    director = Director(bag, places, read_pcd_xyz(args.map))

    if args.preview:
        for value in args.preview.split(","):
            director.frame(start + 2.0 + float(value), start + 2.0).save(args.output.replace(".mp4", f"_{value}.png"))
        return

    encoder = subprocess.Popen(
        [shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{2 * PANE_W}x{2 * PANE_H}", "-r", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "medium",
         "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", args.output], stdin=subprocess.PIPE)
    for index, t in enumerate(np.arange(start, end, 1.0 / FPS)):
        encoder.stdin.write(director.frame(t, start + 2.0).tobytes())
        if index % (10 * FPS) == 0:
            print(f"{t - start:6.1f} / {end - start:.0f} s", flush=True)
    encoder.stdin.close()
    encoder.wait()
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
