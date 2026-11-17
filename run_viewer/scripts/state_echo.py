"""Print the elevator side (/slam_elevatorlio/elevator_side) and door state (/door_state) as JSON lines, each when it changes."""

import json
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String


LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)


class StateEcho(Node):
    """One subscription per state; prints a line when a value changes."""

    def __init__(self) -> None:
        """Subscribe to both topics."""
        super().__init__('run_viewer_state_echo')
        self.values: dict[str, str] = {}
        self.create_subscription(String, '/slam_elevatorlio/elevator_side', lambda message: self.on_value('side', message), LATCHED)
        self.create_subscription(String, '/door_state', lambda message: self.on_value('door', message), 10)

    def on_value(
        self,
        name: str,
        message: String,
    ) -> None:
        """Print the value when it differs from the last one."""
        if self.values.get(name) != message.data:
            self.values[name] = message.data
            print(json.dumps({'name': name, 'value': message.data, 'time': time.time()}), flush=True)


def main() -> None:
    """Spin until killed."""
    rclpy.init()
    rclpy.spin(StateEcho())


if __name__ == '__main__':
    main()
