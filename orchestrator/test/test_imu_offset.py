import math

import numpy as np
import pytest

from orchestrator.imu_offset import calibrate, judge_in_use, offset_at, read_offset_file, store_offset


UPSIDE_DOWN = np.diag([1.0, -1.0, -1.0])
IDENTITY    = np.eye(3)
OFFSET      = np.array([-0.45, -0.02, 0.0])


def _rotation(axis, degrees):
    axis = np.asarray(axis, dtype=float) / np.linalg.norm(axis)
    skew = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    angle = math.radians(degrees)

    return np.eye(3) + math.sin(angle) * skew + (1 - math.cos(angle)) * skew @ skew


def _scene(tilt=_rotation([1, 1, 0], 3.0), offset=OFFSET, floor_noise=0.005, gyro_noise=0.005, seed=1):
    """An upside-down lidar 1.23 m above a floor, the body tilted by tilt; the IMU shares the lidar axes"""
    rng = np.random.default_rng(seed)
    # floor and a wall in the world, seen from the sensor: world -> body is tilt.T, body -> lidar is UPSIDE_DOWN
    floor = np.column_stack([rng.uniform(-3, 3, 40000), rng.uniform(-3, 3, 40000), rng.normal(-1.23, floor_noise, 40000)])
    wall  = np.column_stack([np.full(5000, 2.0), rng.uniform(-3, 3, 5000), rng.uniform(-1.2, 1.0, 5000)])
    world_to_lidar = UPSIDE_DOWN @ tilt.T
    points = np.vstack([floor, wall]) @ world_to_lidar.T
    up_imu = world_to_lidar @ np.array([0.0, 0.0, 1.0])
    acc    = 9.81 * up_imu + offset + rng.normal(0, 0.05, (800, 3))
    gyro   = rng.normal(0, gyro_noise, (800, 3))

    return acc, gyro, points, offset - (offset @ up_imu) * up_imu


def test_the_offset_and_tilt_come_back_from_a_tilted_robot():
    acc, gyro, points, expected = _scene()
    success, message, outputs = calibrate(acc, gyro, points, IDENTITY, UPSIDE_DOWN)

    assert success, message
    assert outputs['imu_acc_offset'] == pytest.approx(expected, abs=0.01)
    assert outputs['tilt_deg'] == pytest.approx(math.degrees(math.atan2(0.4504, 9.81)), abs=0.06)
    assert outputs['floor_height_m'] == pytest.approx(1.23, abs=0.01)
    assert 'tilt 2.6' in message


def test_an_accelerometer_in_g_is_scaled():
    acc, gyro, points, expected = _scene()

    assert calibrate(acc / 9.80665, gyro, points, IDENTITY, UPSIDE_DOWN)[2]['imu_acc_offset'] == pytest.approx(expected, abs=0.01)


def test_the_lidar_to_imu_rotation_is_applied():
    acc, gyro, points, expected = _scene()
    imu_R_lidar = _rotation([0, 0, 1], 90.0)

    outputs = calibrate(acc @ imu_R_lidar.T, gyro, points, imu_R_lidar, UPSIDE_DOWN)[2]

    assert outputs['imu_acc_offset'] == pytest.approx(imu_R_lidar @ expected, abs=0.01)


def test_a_turning_robot_is_rejected():
    success, message, outputs = calibrate(*_scene(gyro_noise=0.2)[:3], IDENTITY, UPSIDE_DOWN)

    assert not success and 'turned' in message and outputs == {}


def test_a_rough_floor_or_no_floor_is_rejected():
    assert 'poor floor fit' in calibrate(*_scene(floor_noise=0.03)[:3], IDENTITY, UPSIDE_DOWN)[1]

    acc, gyro, points, _ = _scene()
    assert 'no floor plane' in calibrate(acc, gyro, points[points[:, 2] < 0.4], IDENTITY, UPSIDE_DOWN)[1]


def test_an_implausible_offset_is_rejected():
    assert 'implausible' in calibrate(*_scene(offset=np.array([2.0, 0.0, 0.0]))[:3], IDENTITY, UPSIDE_DOWN)[1]


def test_the_store_keeps_history_and_a_running_lio_is_judged_by_what_it_read(tmp_path):
    path = tmp_path / 'offset.yaml'
    store_offset({'imu_acc_offset': [-0.45, -0.02, 0.0], 'tilt_deg': 2.6}, path, now=100.0)
    store_offset({'imu_acc_offset': [-0.30, -0.02, 0.0], 'tilt_deg': 1.8}, path, now=200.0)
    content = read_offset_file(path)

    assert content['imu_acc_offset'] == [-0.30, -0.02, 0.0]
    assert [entry['written_at'] for entry in content['history']] == [200.0, 100.0]
    assert offset_at(content['history'], 50.0) == [0.0, 0.0, 0.0]
    assert offset_at(content['history'], 150.0) == [-0.45, -0.02, 0.0]

    assert judge_in_use(content, 250.0)[0]
    success, message, outputs = judge_in_use(content, 150.0)
    assert not success and 'restart SLAM' in message and outputs['change'] == pytest.approx(0.15)
    assert judge_in_use(content, None)[0]
    assert judge_in_use({}, 150.0)[0]
