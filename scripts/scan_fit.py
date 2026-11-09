"""How well the live scan lies on the prior map: run on the NUC in the robot environment, after localization.
   python3 scan_fit.py [MAP.pcd]
Prints the share of /registered_scan points (map frame) within 5, 10, 20, 50 cm of the prior map, then the same with
the scan turned about the robot by -10 to +10 deg. A right match: median about 3 cm, 75 to 80 % within 10 cm, and the
share falling on both sides of 0 deg (N-005)."""
import sys
import math

import numpy as np
import rclpy

from rclpy.node import Node
from nav_msgs.msg import Odometry
from scipy.spatial import cKDTree
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2 as pc2


def read_pcd_xyz(path: str) -> np.ndarray:
    raw    = open(path, 'rb').read()
    header = raw[:raw.index(b'DATA binary\n')].decode()
    fields = next(line.split()[1:] for line in header.splitlines() if line.startswith('FIELDS'))
    count  = int(next(line.split()[1] for line in header.splitlines() if line.startswith('POINTS')))
    start  = raw.index(b'DATA binary\n') + len(b'DATA binary\n')
    data   = np.frombuffer(raw[start:start + count * 4 * len(fields)], dtype=np.float32).reshape(-1, len(fields))
    return data[:, [fields.index(axis) for axis in 'xyz']]


def main():
    rclpy.init()
    node, got = Node('scan_fit_probe'), {}
    node.create_subscription(PointCloud2, '/registered_scan', lambda message: got.__setitem__('scan', message), 5)
    node.create_subscription(Odometry, '/odom_base_link', lambda message: got.__setitem__('odom', message), 5)

    while len(got) < 2:
        rclpy.spin_once(node, timeout_sec=1.0)

    robot = got['odom'].pose.pose.position
    centre = np.array([robot.x, robot.y, 0.0])
    prior = read_pcd_xyz(sys.argv[1] if len(sys.argv) > 1 else '/home/vector/maps/two_floor_clean_cabin2.pcd')
    prior = prior[np.linalg.norm(prior[:, :2] - centre[:2], axis=1) < 25]
    tree  = cKDTree(prior)
    scan  = pc2.read_points_numpy(got['scan'], field_names=('x', 'y', 'z'))[::3]
    print(f'robot ({robot.x:.2f}, {robot.y:.2f}), {len(scan)} scan points, frame {got["scan"].header.frame_id}')

    distance, _ = tree.query(scan)
    print('within 5/10/20/50 cm: ' + ' '.join(f'{np.mean(distance < limit):.3f}' for limit in (0.05, 0.1, 0.2, 0.5))
          + f', median {np.median(distance):.3f} m')

    for degrees in (-10, -5, -2, 0, 2, 5, 10):
        angle    = math.radians(degrees)
        turn     = np.array([[math.cos(angle), -math.sin(angle), 0], [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
        distance, _ = tree.query((scan - centre) @ turn.T + centre)
        print(f'turned {degrees:+3d} deg: within 10 cm {np.mean(distance < 0.1):.3f}, median {np.median(distance):.3f} m')


if __name__ == '__main__':
    main()
