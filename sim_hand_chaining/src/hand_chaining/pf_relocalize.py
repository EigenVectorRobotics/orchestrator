"""Particle-filter relocalization on one floor of the prior map; the result goes to slam_elevatorlio as /initialpose.

python3 pf_relocalize.py --ros-args -p map:=/path/map.pcd -p places:=/path/places.yaml

Request: /pf_relocalize/request (PoseWithCovarianceStamped): the guess; x/y/yaw sigmas from the covariance, the floor
from the guess z. The scan comes from /registered_scan with /state_estimation, so it is right in the sensor frame even
when map <- odom is wrong. The particles live in (x, y, yaw) on a 2D likelihood field of the floor's walls; the filter
resamples on the standing robot's scans with shrinking noise, then sends the best pose to GICP (slam_elevatorlio).
Status JSON on /pf_relocalize/status.
"""
import json
import math
import threading

import numpy as np
import rclpy
import yaml

from geometry_msgs.msg import Pose, PoseArray, PoseWithCovarianceStamped
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.utilities import ok
from scipy.ndimage import distance_transform_edt
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py.point_cloud2 import read_points_numpy
from std_msgs.msg import String

from slam_elevatorlio.map_io import read_pcd_xyz

WALL_BAND = (0.3, 2.0)   # wall points this high above the floor [m]
CELL = 0.05              # likelihood field cell [m]
MAX_DISTANCE = 0.5       # distances are clipped here [m]
SIGMA = 0.1              # point-to-wall noise of the likelihood [m]


def likelihood_field(
    points: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Distance [m] from each cell to the nearest wall point, and the grid origin."""
    origin = points.min(axis=0) - 1.0
    size = np.ceil((points.max(axis=0) + 1.0 - origin) / CELL).astype(int)
    occupied = np.zeros(size, dtype=bool)
    cells = ((points - origin) / CELL).astype(int)
    occupied[cells[:, 0], cells[:, 1]] = True

    return np.minimum(distance_transform_edt(~occupied) * CELL, MAX_DISTANCE), origin


def score(
    particles: np.ndarray,
    scan: np.ndarray,
    field: np.ndarray,
    origin: np.ndarray,
) -> np.ndarray:
    """Log-likelihood of each (x, y, yaw) particle for 2D scan points in the sensor frame."""
    cos, sin = np.cos(particles[:, 2]), np.sin(particles[:, 2])
    x = particles[:, 0, None] + cos[:, None] * scan[None, :, 0] - sin[:, None] * scan[None, :, 1]
    y = particles[:, 1, None] + sin[:, None] * scan[None, :, 0] + cos[:, None] * scan[None, :, 1]
    i = np.clip(((x - origin[0]) / CELL).astype(int), 0, field.shape[0] - 1)
    j = np.clip(((y - origin[1]) / CELL).astype(int), 0, field.shape[1] - 1)
    distance = field[i, j]

    return -(distance ** 2).sum(axis=1) / (2 * SIGMA ** 2)


def run_filter(
    guess: np.ndarray,
    sigmas: np.ndarray,
    scan: np.ndarray,
    field: np.ndarray,
    origin: np.ndarray,
    rng: np.random.Generator,
    count: int = 6000,
    iterations: int = 25,
    show=None,
) -> tuple[np.ndarray, float, float]:
    """Sample around the guess, then resample with shrinking roughening noise; best pose, spread [m], fit [m].

    show(particles) is called before each iteration (for the video).
    """
    particles = guess + rng.normal(0.0, 1.0, (count, 3)) * sigmas
    weights = np.full(count, 1.0 / count)
    for iteration in range(iterations):
        if show is not None:
            show(particles)
        log = score(particles, scan, field, origin)
        weights = np.exp(log - log.max())
        weights /= weights.sum()
        picks = rng.choice(count, size=count, p=weights)
        shrink = max(0.05, 0.75 ** iteration)
        particles = particles[picks] + rng.normal(0.0, 1.0, (count, 3)) * np.maximum(sigmas * 0.2 * shrink, [0.01, 0.01, 0.005])

    log = score(particles, scan, field, origin)
    best = particles[np.argmax(log)]
    spread = float(np.sqrt(np.mean(np.sum((particles[:, :2] - best[:2]) ** 2, axis=1))))
    fit = float(np.sqrt(-2 * SIGMA ** 2 * log.max() / len(scan)))

    return best, spread, fit


class PfRelocalize(Node):
    def __init__(self) -> None:
        super().__init__("pf_relocalize")

        with open(self.declare_parameter("places", "").value) as file:
            self.floors = yaml.safe_load(file)["floors"]
        self.sensor_height = self.declare_parameter("sensor_height_m", 1.23).value
        self.scan_points = self.declare_parameter("scan_points", 400).value
        self.rng = np.random.default_rng(0)

        points = read_pcd_xyz(self.declare_parameter("map", "").value)
        self.fields = {}
        for name, floor in self.floors.items():
            band = (points[:, 2] > floor["ground_z"] + WALL_BAND[0]) & (points[:, 2] < floor["ground_z"] + WALL_BAND[1])
            self.fields[name] = likelihood_field(points[band, :2])
        self.get_logger().info(f"likelihood fields for floors {list(self.fields)}")

        self.lock = threading.Lock()
        self.sensor = None
        self.scans = []
        self.initial_pub = self.create_publisher(PoseWithCovarianceStamped, "/initialpose", 1)
        self.status_pub = self.create_publisher(String, "/pf_relocalize/status", 1)
        self.particles_pub = self.create_publisher(PoseArray, "/pf_relocalize/particles", 30)
        self.create_subscription(Odometry, "/state_estimation", self.on_state, 10)
        self.create_subscription(PointCloud2, "/registered_scan", self.on_scan, 5)
        self.create_subscription(PoseWithCovarianceStamped, "/pf_relocalize/request", self.on_request, 1)

    def on_state(
        self,
        message: Odometry,
    ) -> None:
        pose = message.pose.pose
        x, y, z, w = pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w
        rotation = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                             [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                             [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
        with self.lock:
            self.sensor = (rotation, np.array([pose.position.x, pose.position.y, pose.position.z]))

    def on_scan(
        self,
        cloud: PointCloud2,
    ) -> None:
        """Keep the last 5 scans as 2D wall points in a gravity-aligned sensor frame."""
        with self.lock:
            if self.sensor is None:
                return
            rotation, position = self.sensor
        offset = read_points_numpy(cloud, field_names=["x", "y", "z"], skip_nans=True).astype(float) - position
        yaw = math.atan2(rotation[1, 0], rotation[0, 0])
        cos, sin = math.cos(-yaw), math.sin(-yaw)
        flat = np.stack([cos * offset[:, 0] - sin * offset[:, 1], sin * offset[:, 0] + cos * offset[:, 1], offset[:, 2]], 1)
        heights = flat[:, 2] + self.sensor_height
        walls = flat[(heights > WALL_BAND[0]) & (heights < WALL_BAND[1]) & (np.hypot(flat[:, 0], flat[:, 1]) > 0.5), :2]
        with self.lock:
            self.scans = (self.scans + [walls])[-5:]

    def on_request(
        self,
        message: PoseWithCovarianceStamped,
    ) -> None:
        threading.Thread(target=self.relocalize, args=(message,), daemon=True).start()

    def relocalize(
        self,
        message: PoseWithCovarianceStamped,
    ) -> None:
        """One filter run on the latest scans; publish the pose on /initialpose if the fit is good."""
        pose, covariance = message.pose.pose, message.pose.covariance
        floor = min(self.floors, key=lambda name: abs(self.floors[name]["ground_z"] + self.sensor_height - pose.position.z))
        guess = np.array([pose.position.x, pose.position.y, 2.0 * math.atan2(pose.orientation.z, pose.orientation.w)])
        sigmas = np.sqrt(np.maximum([covariance[0], covariance[7], covariance[35]], [0.25, 0.25, 0.1]))

        with self.lock:
            scan = np.concatenate(self.scans) if self.scans else np.zeros((0, 2))
        if len(scan) < 50:
            self.publish_status({"state": "failed", "reason": f"only {len(scan)} wall points in the scan"})
            return
        scan = scan[self.rng.choice(len(scan), size=min(self.scan_points, len(scan)), replace=False)]

        field, origin = self.fields[floor]
        z = float(self.floors[floor]["ground_z"] + self.sensor_height)
        best, spread, fit = run_filter(guess, sigmas, scan, field, origin, self.rng,
                                       show=lambda particles: self.publish_particles(particles, z))
        result = {"floor": floor, "x": round(float(best[0]), 3), "y": round(float(best[1]), 3),
                  "yaw_deg": round(math.degrees(best[2]), 1), "spread_m": round(spread, 3), "fit_m": round(fit, 3)}
        if fit > 0.15 or spread > 0.3:
            self.publish_status({"state": "failed", "reason": "no clear pose", **result})
            return

        output = PoseWithCovarianceStamped()
        output.header.frame_id, output.header.stamp = "map", self.get_clock().now().to_msg()
        output.pose.pose.position.x, output.pose.pose.position.y, output.pose.pose.position.z = float(best[0]), float(best[1]), z
        output.pose.pose.orientation.z, output.pose.pose.orientation.w = math.sin(best[2] / 2), math.cos(best[2] / 2)
        self.initial_pub.publish(output)
        self.publish_status({"state": "done", **result})

    def publish_particles(
        self,
        particles: np.ndarray,
        z: float,
    ) -> None:
        """300 of the (x, y, yaw) particles as a PoseArray in the map frame."""
        message = PoseArray()
        message.header.frame_id, message.header.stamp = "map", self.get_clock().now().to_msg()
        for x, y, yaw in particles[:: max(1, len(particles) // 300)]:
            pose = Pose()
            pose.position.x, pose.position.y, pose.position.z = float(x), float(y), z
            pose.orientation.z, pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
            message.poses.append(pose)
        self.particles_pub.publish(message)

    def publish_status(
        self,
        status: dict,
    ) -> None:
        self.status_pub.publish(String(data=json.dumps(status)))
        self.get_logger().info(f"pf_relocalize: {status}")


def main() -> None:
    """Spin until Ctrl-C."""
    rclpy.init()
    node = PfRelocalize()

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
