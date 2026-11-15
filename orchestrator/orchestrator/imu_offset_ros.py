import argparse
import sys
import time

import numpy as np
import rclpy

from pathlib import Path
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.utilities import get_message
from sensor_msgs.msg import Imu
from sensor_msgs_py import point_cloud2

from .imu_offset import OFFSET_FILE, SENSOR_CONFIG, calibrate, load_extrinsics, store_offset


IMU_TOPIC   = '/imu/data'
LIDAR_TOPIC = '/lidar/scan'
MAX_SCANS   = 20


def scan_points(message) -> np.ndarray:
    """xyz (N x 3) of a Livox CustomMsg or a PointCloud2"""
    if hasattr(message, 'points'):
        return np.array([(point.x, point.y, point.z) for point in message.points], dtype=float).reshape(-1, 3)

    return point_cloud2.read_points_numpy(message, field_names=('x', 'y', 'z')).astype(float)


def lidar_type(node, wait_s):
    deadline = time.monotonic() + wait_s

    while time.monotonic() < deadline:
        types = dict(node.get_topic_names_and_types()).get(LIDAR_TOPIC)
        if types:
            return types[0]
        rclpy.spin_once(node, timeout_sec=0.2)

    return None


def collect(seconds):
    """IMU samples and lidar scans over seconds, taken while the robot stands"""
    rclpy.init()
    node = Node('imu_offset_calibration')
    acc, gyro, scans = [], [], []

    try:
        scan_type = lidar_type(node, 5.0)
        if scan_type is None:
            return None, f'no publisher of {LIDAR_TOPIC}'

        def on_imu(message):
            acc.append((message.linear_acceleration.x, message.linear_acceleration.y, message.linear_acceleration.z))
            gyro.append((message.angular_velocity.x, message.angular_velocity.y, message.angular_velocity.z))

        def on_scan(message):
            if len(scans) < MAX_SCANS:
                scans.append(scan_points(message))

        node.create_subscription(Imu, IMU_TOPIC, on_imu, qos_profile_sensor_data)
        node.create_subscription(get_message(scan_type), LIDAR_TOPIC, on_scan, qos_profile_sensor_data)
        deadline = time.monotonic() + seconds

        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_node()
        rclpy.shutdown()

    if not scans:
        return None, f'no scan on {LIDAR_TOPIC} in {seconds:g} s'

    return (np.array(acc, dtype=float).reshape(-1, 3), np.array(gyro, dtype=float).reshape(-1, 3), np.vstack(scans)), ''


def main():
    """python3 -m orchestrator.imu_offset_ros: measure the offset at rest and store it; exit 1 when rejected"""
    parser = argparse.ArgumentParser(description='Measure the Mid-360 accelerometer offset with the robot standing still')
    parser.add_argument('--seconds', type=float, default=4.0)
    parser.add_argument('--file', type=Path, default=OFFSET_FILE)
    parser.add_argument('--sensor-config', type=Path, default=SENSOR_CONFIG)
    args = parser.parse_args()

    data, problem = collect(args.seconds)
    if data is None:
        print(f'rejected: {problem}; kept the stored offset')
        sys.exit(1)

    success, message, outputs = calibrate(*data, *load_extrinsics(args.sensor_config))
    if not success:
        print(f'rejected: {message}; kept the stored offset')
        sys.exit(1)

    store_offset(outputs, args.file)
    print(f'{message}; stored')


if __name__ == '__main__':
    main()
