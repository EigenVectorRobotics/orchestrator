import sys
import json
import argparse

import rclpy

from pathlib import Path

from std_msgs.msg import String
from skill_registry import load_tree

from .topics import TREE_BLIND_TOPIC, TREE_TOPIC


def main():
    parser = argparse.ArgumentParser(description='Send a tree file to the executor')
    parser.add_argument('tree', type=Path)
    parser.add_argument('--blind', action='store_true', help='validate it as a tree that must run with no cloud link')
    parser.add_argument('--skip-preflight', action='store_true',
                        help='run it although no preflight has passed since the launch (tests); the trace marks it')
    arguments = parser.parse_args()
    tree      = load_tree(arguments.tree)              # fails here, before ROS, on a missing or cyclic include

    rclpy.init()
    node      = rclpy.create_node('send_tree')
    topic     = TREE_BLIND_TOPIC if arguments.blind else TREE_TOPIC
    publisher = node.create_publisher(String, topic, 10)

    for _ in range(30):                                 # wait up to 3 s for the executor
        if publisher.get_subscription_count() > 0:
            break

        rclpy.spin_once(node, timeout_sec=0.1)
    else:
        print(f'no executor subscribes to {topic}', file=sys.stderr)
        sys.exit(1)

    data = json.dumps({'tree': tree, 'skip_preflight': True}) if arguments.skip_preflight else tree
    publisher.publish(String(data=data))
    rclpy.spin_once(node, timeout_sec=0.5)

    node.destroy_node()
    rclpy.try_shutdown()


if __name__ == '__main__':
    main()
