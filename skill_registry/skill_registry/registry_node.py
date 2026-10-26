import json
import time
import threading
import urllib.request

import yaml
import rclpy

from pathlib import Path

from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from ament_index_python.packages import get_package_share_directory

from .contract import ContractError
from .registry import GraphSnapshot, Registry, is_present


# Volatile, so it matches any publisher. Skill servers announce again every few seconds
ANNOUNCE_QOS = QoSProfile(depth=100, durability=DurabilityPolicy.VOLATILE)
LIVE_QOS     = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)


class RegistryNode(Node):
    """Publish which skills are live: found in the ROS graph, or announced by a skill server"""

    def __init__(self):
        super().__init__('skill_registry')

        default_directory = Path(get_package_share_directory('skill_registry')) / 'contracts'

        self.contracts_directory = self.declare_parameter('contracts_directory', str(default_directory)).value
        self.max_age_s           = self.declare_parameter('max_age_s', 5.0).value
        self.vlm_base_url        = self.declare_parameter('vlm_base_url', 'http://vector-thor:8090/v1').value
        self.vlm_up              = False

        self.registry = Registry.from_directory(Path(self.contracts_directory))
        self._live    = ''

        self._live_publisher = self.create_publisher(String, '/skills/live', LIVE_QOS)
        self._subscription   = self.create_subscription(String, '/skills/announce', self._on_announce, ANNOUNCE_QOS)
        self._live_timer     = self.create_timer(1.0, self._publish_live)

        threading.Thread(target=self._probe_vlm_forever, daemon=True).start()

        self.get_logger().info(f'{len(self.registry)} contracts from {self.contracts_directory}')

    def _on_announce(self, message: String):
        try:
            raw = yaml.safe_load(message.data)
            was_live = self.registry.is_live(raw.get('name', ''), self.max_age_s)
            contract = self.registry.announce(raw)
        except (ContractError, TypeError, AttributeError, yaml.YAMLError) as error:
            self.get_logger().error(f'bad announcement: {error}')
            return

        if not was_live:
            self.get_logger().info(f'skill up: {contract.name} on {contract.device}')

    def _probe_vlm_forever(self):
        while rclpy.ok():
            try:
                with urllib.request.urlopen(f'{self.vlm_base_url.rstrip("/")}/models', timeout=2.0):
                    self.vlm_up = True
            except OSError:
                self.vlm_up = False

            time.sleep(5.0)

    def _graph_snapshot(self) -> GraphSnapshot:
        """Query the graph once per name the contracts use, not once per contract and check"""
        services, published, subscribed = set(), set(), set()

        for contract in self.registry:
            interface = contract.interface
            name      = interface.name if interface.name.startswith('/') else f'/{interface.name}'

            if interface.transport == 'action':
                services.add(f'{name}/_action/send_goal')
            elif interface.transport == 'service':
                services.add(name)
            elif interface.transport == 'topic':
                subscribed.add(name)

            if interface.success_when is not None:
                published.add(interface.success_when.topic)

        return GraphSnapshot(
            services={name for name in services if self.count_services(name) > 0},
            published={name for name in published if self.count_publishers(name) > 0},
            subscribed={name for name in subscribed if self.count_subscribers(name) > 0}
        )

    def _publish_live(self):
        graph     = self._graph_snapshot()
        announced = {contract.name for contract in self.registry.live(self.max_age_s)}
        present   = {contract.name for contract in self.registry if is_present(contract, graph, self.vlm_up)}
        payload   = json.dumps(sorted(announced | present))

        if payload == self._live:
            return

        self._live = payload
        self._live_publisher.publish(String(data=payload))


def main():
    rclpy.init()
    node = RegistryNode()

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
