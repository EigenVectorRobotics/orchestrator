"""Mapping operator for the sim: walk the G1 through both floors and the elevator while Elevator-LIO maps, then save the map.

python3 mapping_tour.py --ros-args -p output:=/path/sim_map.pcd

Stands in for the person with the joystick: /sim/goto (privileged A*) for walking and /sim/call for pressing calls.
Also writes <output stem>_frame.yaml: the map frame in the sim world frame (the sensor pose when LIO started, yaw only).
"""
import json
import math
import time

import rclpy
import yaml

from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.utilities import ok
from std_msgs.msg import Int8, String

from navigation_interfaces.srv import SaveMap

# (kind, arguments) in the sim world frame. Floor 0 is the ground floor; the car waits on floor 1 at start.
TOUR = [
    ("call", 0), ("goto", 1.6, -5.0, -1.57), ("goto", 4.5, -6.5, 0.0), ("goto", 4.5, -2.0, 1.57),
    ("goto", 4.0, 1.5, 3.14), ("goto", 1.0, -2.0, 3.14), ("goto", -2.5, -6.5, 3.14), ("goto", -4.5, -4.0, 1.57),
    ("goto", -1.0, -4.5, 0.0), ("open", 0), ("goto", 0.0, -1.0, 1.57), ("goto", 0.0, 1.4, 1.57),
    ("goto", 0.0, 1.4, -1.57), ("call", 1), ("open", 1), ("goto", 0.0, -1.0, -1.57), ("goto", 1.6, -5.0, -1.57),
    ("goto", 4.5, -6.5, 0.0), ("goto", 4.5, -2.0, 1.57), ("goto", 4.0, 1.5, 3.14), ("goto", 1.0, -2.0, 3.14),
    ("goto", -2.5, -6.5, 3.14), ("goto", -4.5, -4.0, 1.57), ("goto", -1.0, -4.5, 0.0), ("goto", 0.0, -1.5, 1.57),
]


class MappingTour(Node):
    def __init__(self) -> None:
        super().__init__("mapping_tour")

        self.output = self.declare_parameter("output", "").get_parameter_value().string_value
        self.goto_status, self.state, self.sensor = None, {}, None

        self.goto_pub = self.create_publisher(PoseStamped, "/sim/goto", 10)
        self.call_pub = self.create_publisher(Int8, "/sim/call", 10)
        self.create_subscription(String, "/sim/goto_status", self.on_goto_status, 10)
        self.create_subscription(String, "/sim/state", lambda message: setattr(self, "state", json.loads(message.data)), 10)
        self.create_subscription(Odometry, "/sim/sensor_truth", self.on_sensor, 10)
        self.save_client = self.create_client(SaveMap, "/slam_elevatorlio/save_map")

    def on_goto_status(
        self,
        message: String,
    ) -> None:
        self.goto_status = message.data

    def on_sensor(
        self,
        message: Odometry,
    ) -> None:
        if self.sensor is None:
            pose = message.pose.pose
            yaw = math.atan2(2 * (pose.orientation.w * pose.orientation.z + pose.orientation.x * pose.orientation.y),
                             1 - 2 * (pose.orientation.y ** 2 + pose.orientation.z ** 2))
            self.sensor = {"x": pose.position.x, "y": pose.position.y, "z": pose.position.z, "yaw": yaw}

    def wait(
        self,
        condition,
        timeout: float,
    ) -> bool:
        """Spin until condition() is true or timeout [s] passes."""
        end = time.monotonic() + timeout
        while ok() and time.monotonic() < end:
            rclpy.spin_once(self, timeout_sec=0.1)
            if condition():
                return True

        return False

    def goto(
        self,
        x: float,
        y: float,
        yaw: float,
    ) -> str:
        """One privileged walk; returns arrived or blocked."""
        goal = PoseStamped()
        goal.header.frame_id = "world"
        goal.pose.position.x, goal.pose.position.y = x, y
        goal.pose.orientation.z, goal.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        self.goto_status = None
        self.goto_pub.publish(goal)
        self.wait(lambda: self.goto_status in ("arrived", "blocked"), 120.0)

        return self.goto_status or "timeout"

    def run(self) -> None:
        self.wait(lambda: self.sensor is not None and self.goto_pub.get_subscription_count() > 0, 60.0)
        self.wait(lambda: False, 5.0)

        for step in TOUR:
            kind, arguments = step[0], step[1:]
            if kind == "goto":
                result = self.goto(*arguments)
            elif kind == "call":
                self.call_pub.publish(Int8(data=arguments[0]))
                result = "called"
            else:
                result = "open" if self.wait(lambda: self.state.get("elevator_floor") == arguments[0] and
                                             self.state.get("elevator_state") == "open", 90.0) else "timeout"
            self.get_logger().info(f"{kind} {arguments}: {result} (robot {self.state.get('robot')})")

        self.wait(lambda: False, 3.0)
        self.save()

    def save(self) -> None:
        """Save the LIO map and the map frame."""
        request = SaveMap.Request(path=self.output)
        self.save_client.wait_for_service(timeout_sec=10.0)
        future = self.save_client.call_async(request)
        self.wait(future.done, 60.0)
        self.get_logger().info(f"save_map: {future.result().message if future.done() else 'no answer'}")

        with open(self.output.removesuffix(".pcd") + "_frame.yaml", "w") as file:
            yaml.safe_dump({"world_from_map": self.sensor, "note": "map = the sim sensor pose when LIO started, yaw only"}, file)


def main() -> None:
    """Run the tour once."""
    rclpy.init()
    node = MappingTour()

    try:
        node.run()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
