"""Elevator door state node: count scan points inside a rectangle and publish OPEN or CLOSED on /door_state.

python3 door_state.py --ros-args -p config:=/path/to/elevator_door_state.yaml

The YAML holds corners (4 x [x, y] in the map frame, in order around the rectangle), min_intensity and threshold.
The scans (/registered_scan) must be in the same map frame as the corners.
"""
import numpy as np
import rclpy
import yaml

from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.utilities import ok
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py.point_cloud2 import read_points_numpy
from std_msgs.msg import String

from utils import count_points_in_rectangle


class DoorState(Node):
    def __init__(self) -> None:
        super().__init__("door_state")

        with open(self.declare_parameter("config", "").get_parameter_value().string_value) as file:
            config = yaml.safe_load(file)

        self.corners = np.array(config["corners"], dtype=float)
        self.min_intensity = config["min_intensity"]
        self.threshold = config["threshold"]

        self.publisher = self.create_publisher(String, "/door_state", 10)
        self.create_subscription(PointCloud2, "/registered_scan", self.on_scan, 10)

    def on_scan(
        self,
        cloud: PointCloud2,
    ) -> None:
        """Publish OPEN when at least threshold points lie in the rectangle, CLOSED otherwise."""
        points = read_points_numpy(cloud, field_names=["x", "y", "z", "intensity"], skip_nans=True)
        count = count_points_in_rectangle(points, self.corners, self.min_intensity)
        self.publisher.publish(String(data="OPEN" if count >= self.threshold else "CLOSED"))


def main() -> None:
    """Spin the node until Ctrl-C."""
    rclpy.init()
    node = DoorState()

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
