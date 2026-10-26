import sys
import argparse

import rclpy

from pathlib import Path

from std_msgs.msg import String


def main():
    parser = argparse.ArgumentParser(description='Send a tree file to the executor')
    parser.add_argument('tree', type=Path)
    parser.add_argument('--blind', action='store_true', help='validate it as a tree that must run with no cloud link')
    arguments = parser.parse_args()

    rclpy.init()
    node      = rclpy.create_node('send_tree')
    topic     = '/orchestrator/tree_blind' if arguments.blind else '/orchestrator/tree'
    publisher = node.create_publisher(String, topic, 10)

    for _ in range(30):                                 # wait up to 3 s for the executor
        if publisher.get_subscription_count() > 0:
            break

        rclpy.spin_once(node, timeout_sec=0.1)
    else:
        print(f'no executor subscribes to {topic}', file=sys.stderr)
        sys.exit(1)

    publisher.publish(String(data=arguments.tree.read_text()))
    rclpy.spin_once(node, timeout_sec=0.5)

    node.destroy_node()
    rclpy.try_shutdown()


if __name__ == '__main__':
    main()
