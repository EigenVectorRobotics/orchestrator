import math
import os
import sys
import time

from datetime import datetime
from pathlib import Path

import numpy as np
import yaml


G_M_S2         = 9.80665
OFFSET_FILE    = Path.home() / '.ros' / 'elevator_lio_imu_acc_offset.yaml'
SENSOR_CONFIG  = (Path.home() / 'eigen_vector_ws' / 'src' / 'navigation_stack' / 'slam_elevatorlio' / 'config'
                  / 'elevator_lio' / 'sensor_unitree_g1.yaml')
LIO_EXECUTABLE = '/lio/lib/lio/lio'
HISTORY_LENGTH = 20
LIMITS         = {
    'max_gyro_std_rad_s':  0.05,       # a standing G1 sways; a step or a turn is far above it
    'max_gyro_mean_rad_s': 0.05,
    'max_acc_std_m_s2':    0.4,
    'min_imu_samples':     200,
    'floor_min_range_m':   0.3,
    'floor_max_range_m':   3.0,
    'floor_min_depth_m':   0.4,        # along the sensor's down axis: the floor is 1.2 m below the head
    'floor_max_tilt_deg':  20.0,
    'floor_gate_m':        0.02,
    'min_floor_inliers':   2000,
    'max_floor_rms_m':     0.010,      # the 2 cm gate clips a rough floor's rms near 11 mm; a flat one fits to 6-8 mm
    'max_offset_m_s2':     1.5,        # 9 deg: more is a bad fit or a robot that leans on something
    'warn_change_m_s2':    0.05,       # 0.3 deg of odom tilt
}


def load_extrinsics(path: Path = SENSOR_CONFIG) -> tuple[np.ndarray, np.ndarray]:
    """imu_R_lidar and lidar_R_body from Elevator-LIO's sensor config"""
    offset = yaml.safe_load(open(path))['offset']

    return np.array(offset['imu_R_lidar'], dtype=float), np.array(offset['lidar_R_body'], dtype=float)


def judge_rest(acc: np.ndarray, gyro: np.ndarray, limits: dict = LIMITS) -> tuple[bool, str]:
    """acc and gyro (N x 3) say the robot stood still"""
    if len(acc) < limits['min_imu_samples']:
        return False, f'only {len(acc)} IMU samples (needs {limits["min_imu_samples"]})'

    gyro_std, gyro_mean = float(gyro.std(axis=0).max()), float(np.linalg.norm(gyro.mean(axis=0)))
    acc_std             = float(acc.std(axis=0).max())

    if gyro_std > limits['max_gyro_std_rad_s'] or gyro_mean > limits['max_gyro_mean_rad_s']:
        return False, f'the robot turned or swayed: gyro std {gyro_std:.3f}, mean {gyro_mean:.3f} rad/s'

    if acc_std > limits['max_acc_std_m_s2']:
        return False, f'the robot moved: accelerometer std {acc_std:.3f} m/s^2'

    return True, f'at rest: gyro std {gyro_std:.3f} rad/s, accelerometer std {acc_std:.3f} m/s^2'


def fit_floor(points: np.ndarray, down: np.ndarray, limits: dict = LIMITS, seed: int = 0) -> dict | None:
    """The floor plane below the sensor in the lidar points (N x 3, lidar frame); its normal points down"""
    # (1) Candidates: points well below the sensor, off the robot's own body
    depth  = points @ down
    radial = np.linalg.norm(points - np.outer(depth, down), axis=1)
    cand   = points[(depth > limits['floor_min_depth_m']) & (radial > limits['floor_min_range_m'])
                    & (radial < limits['floor_max_range_m'])]

    if len(cand) < limits['min_floor_inliers']:
        return None

    # (2) RANSAC on a subsample, planes within floor_max_tilt_deg of the down axis
    rng     = np.random.default_rng(seed)
    sample  = cand[rng.choice(len(cand), min(len(cand), 10000), replace=False)]
    triples = sample[rng.integers(0, len(sample), (300, 3))]
    normals = np.cross(triples[:, 1] - triples[:, 0], triples[:, 2] - triples[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    keep    = lengths > 1e-6
    normals = normals[keep] / lengths[keep, None]
    normals *= np.sign(normals @ down)[:, None]
    upright = normals @ down > math.cos(math.radians(limits['floor_max_tilt_deg']))
    normals, anchors = normals[upright], triples[keep][upright, 0]

    if not len(normals):
        return None

    scores = (np.abs(sample @ normals.T - (anchors * normals).sum(axis=1)) < limits['floor_gate_m']).sum(axis=0)
    normal, anchor = normals[scores.argmax()], anchors[scores.argmax()]

    # (3) Refine on all candidates: least squares on the inliers, twice
    for _ in range(2):
        inliers = cand[np.abs((cand - anchor) @ normal) < limits['floor_gate_m']]
        anchor  = inliers.mean(axis=0)
        normal  = np.linalg.svd(inliers - anchor, full_matrices=False)[2][2]
        normal *= np.sign(normal @ down)

    return {
        'normal':  normal,
        'height':  float(anchor @ normal),
        'inliers': len(inliers),
        'rms':     float(np.std((inliers - anchor) @ normal)),
        'share':   len(inliers) / len(cand),
    }


def accel_offset(acc_mean: np.ndarray, up: np.ndarray) -> dict:
    """The accelerometer offset at rest: the mean minus its part along the floor's up axis (both in the IMU frame)"""
    along  = float(acc_mean @ up)
    offset = acc_mean - along * up
    tilt   = math.degrees(math.atan2(float(np.linalg.norm(offset)), along))

    return {'offset': offset, 'tilt_deg': tilt, 'acc_along_up': along}


def calibrate(acc: np.ndarray, gyro: np.ndarray, points: np.ndarray, imu_R_lidar: np.ndarray, lidar_R_body: np.ndarray,
              limits: dict = LIMITS) -> tuple[bool, str, dict]:
    """Accelerometer offset in the IMU frame from IMU samples and lidar points taken at rest"""
    # (1) The robot must stand still
    still, rest_text = judge_rest(acc, gyro, limits)

    if not still:
        return False, rest_text, {}

    # (2) The floor below the sensor; body is upright, so down in the lidar frame is lidar_R_body (0, 0, -1)
    down  = lidar_R_body @ np.array([0.0, 0.0, -1.0])
    floor = fit_floor(points, down, limits)

    if floor is None:
        return False, f'no floor plane below the sensor in {len(points)} lidar points', {}

    if floor['inliers'] < limits['min_floor_inliers'] or floor['rms'] > limits['max_floor_rms_m']:
        return False, f'poor floor fit: {floor["inliers"]} inliers, rms {floor["rms"] * 1000:.1f} mm', {}

    # (3) Gravity at rest reads +g along up; the accelerometer may report in g
    acc_mean = acc.mean(axis=0)
    acc_mean = acc_mean * G_M_S2 if np.linalg.norm(acc_mean) < 1.2 else acc_mean
    result   = accel_offset(acc_mean, imu_R_lidar @ -floor['normal'])
    norm     = float(np.linalg.norm(result['offset']))

    if norm > limits['max_offset_m_s2']:
        return False, f'offset {norm:.2f} m/s^2 is implausible (tilt {result["tilt_deg"]:.2f} deg)', {}

    outputs = {
        'imu_acc_offset': [round(float(value), 4) for value in result['offset']],
        'offset_norm':    round(norm, 4),
        'tilt_deg':       round(result['tilt_deg'], 3),
        'acc_along_up':   round(result['acc_along_up'], 4),
        'acc_mean':       [round(float(value), 4) for value in acc_mean],
        'floor_normal':   [round(float(value), 5) for value in floor['normal']],
        'floor_height_m': round(floor['height'], 3),
        'floor_inliers':  floor['inliers'],
        'floor_rms_m':    round(floor['rms'], 4),
        'floor_share':    round(floor['share'], 3),
        'imu_samples':    len(acc),
    }
    offset_text = ', '.join(f'{value:+.3f}' for value in outputs['imu_acc_offset'])

    return True, (f'{rest_text}; floor {floor["inliers"]} inliers, rms {floor["rms"] * 1000:.1f} mm; '
                  f'offset ({offset_text}) m/s^2, tilt {outputs["tilt_deg"]:.2f} deg'), outputs


def read_offset_file(path: Path = OFFSET_FILE) -> dict:
    try:
        return yaml.safe_load(open(path)) or {}
    except (OSError, yaml.YAMLError):
        return {}


def store_offset(outputs, path=OFFSET_FILE, now=None):
    """The new offset on top, where Elevator-LIO reads it at start; the history tells which one a running LIO has"""
    now      = time.time() if now is None else now
    previous = read_offset_file(path)
    entry    = {'written_at': round(now, 3), 'imu_acc_offset': outputs['imu_acc_offset'], 'tilt_deg': outputs['tilt_deg']}
    content  = {
        'imu_acc_offset': outputs['imu_acc_offset'],
        'written_at':     entry['written_at'],
        'written':        datetime.fromtimestamp(now).isoformat(timespec='seconds'),
        'measurement':    {key: value for key, value in outputs.items() if key != 'imu_acc_offset'},
        'history':        ([entry] + previous.get('history', []))[:HISTORY_LENGTH],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text('# Accelerometer offset of the Mid-360 IMU [m/s^2, IMU frame], measured by preflight '
                         '(orchestrator imu_offset).\n# Elevator-LIO reads imu_acc_offset once at start: '
                         'a new value needs a SLAM restart.\n' + yaml.safe_dump(content, sort_keys=False))
    os.replace(temporary, path)


def offset_at(history: list, started_at: float) -> list:
    """The offset a LIO started at started_at read: the newest one written before it, zero before any"""
    before = [entry for entry in history if entry['written_at'] < started_at]

    return max(before, key=lambda entry: entry['written_at'])['imu_acc_offset'] if before else [0.0, 0.0, 0.0]


def judge_in_use(content: dict, lio_started_at: float | None, limits: dict = LIMITS) -> tuple[bool, str, dict]:
    """The offset the running LIO started with is within warn_change_m_s2 of the last one measured"""
    if not content.get('imu_acc_offset'):
        return True, 'no offset measured yet', {}

    measured = np.array(content['imu_acc_offset'], dtype=float)

    if lio_started_at is None:
        return True, 'Elevator-LIO does not run: it will start with the stored offset', {}

    in_use  = np.array(offset_at(content.get('history', []), lio_started_at), dtype=float)
    change  = float(np.linalg.norm(measured - in_use))
    outputs = {'in_use': in_use.round(4).tolist(), 'measured': measured.round(4).tolist(), 'change': round(change, 4)}
    text    = f'SLAM uses ({", ".join(f"{v:+.3f}" for v in in_use)}), measured ({", ".join(f"{v:+.3f}" for v in measured)}) m/s^2'

    if change > limits['warn_change_m_s2']:
        return False, f'{text}: {change:.3f} m/s^2 apart; restart SLAM to use the new offset', outputs

    return True, f'{text}: {change:.3f} m/s^2 apart', outputs


def lio_started_at():
    """Wall time at which the running Elevator-LIO process started, None when none runs"""
    boot  = next(float(line.split()[1]) for line in open('/proc/stat') if line.startswith('btime'))
    ticks = os.sysconf('SC_CLK_TCK')

    for pid in filter(str.isdigit, os.listdir('/proc')):
        try:
            command = open(f'/proc/{pid}/cmdline', 'rb').read().split(b'\0')[0].decode()
            if command.endswith(LIO_EXECUTABLE):
                stat = open(f'/proc/{pid}/stat').read()
                return boot + int(stat[stat.rindex(')') + 2:].split()[19]) / ticks
        except OSError:
            continue

    return None


def main():
    """python3 -m orchestrator.imu_offset check: exit 0 when the running LIO has the last measured offset"""
    if sys.argv[1:] != ['check']:
        sys.exit('usage: python3 -m orchestrator.imu_offset check')

    success, message, _ = judge_in_use(read_offset_file(), lio_started_at())
    print(message)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
