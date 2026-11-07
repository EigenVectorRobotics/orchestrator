import math

import numpy as np
import pytest

from orchestrator.door_checks import judge_doorway, move_points


DOOR  = {'x': 2.0, 'y': -1.0, 'yaw': math.pi / 2}          # doorway along x, out of the cabin towards +y
ROBOT = (2.0, -2.0, -0.45)                                  # 1 m inside, base_link at z -0.45


def _wall(count: int, y: float = -1.0) -> np.ndarray:
    """A wall of points across the doorway at y, from 0.2 to 1.0 m above base_link"""
    rng = np.random.default_rng(0)
    return np.column_stack([rng.uniform(1.7, 2.3, count), np.full(count, y), ROBOT[2] + rng.uniform(0.2, 1.0, count)])


def _room(count: int = 2000) -> np.ndarray:
    """The cabin walls and floor, away from the doorway"""
    rng = np.random.default_rng(1)
    return np.column_stack([rng.uniform(0.0, 4.0, count), rng.uniform(-4.0, -1.6, count), rng.uniform(-1.2, 1.0, count)])


def test_a_shut_door_fills_the_doorway():
    is_open, message, outputs = judge_doorway(np.vstack([_room(), _wall(400)]), DOOR, ROBOT)

    assert not is_open and message.startswith('closed: 400 points')
    assert outputs['distance_m'] == pytest.approx(1.0)


def test_an_open_door_leaves_the_doorway_empty():
    beyond = _wall(400, y=0.5)                              # the landing's far wall, 1.5 m past the door
    is_open, message, outputs = judge_doorway(np.vstack([_room(), beyond]), DOOR, ROBOT)

    assert is_open and outputs['points_in_doorway'] == 0


def test_points_at_the_floor_or_beside_the_frame_do_not_count():
    floor = _wall(300) - np.array([0.0, 0.0, 0.5])         # 0.3 m below base_link to 0.5 above: some below min_up_m
    jamb  = _wall(300) + np.array([1.0, 0.0, 0.0])         # 0.7 to 1.3 m to the side
    _, _, outputs = judge_doorway(np.vstack([_room(), floor, jamb]), DOOR, ROBOT)

    assert outputs['points_in_doorway'] < 300


def test_no_judgement_without_scans_a_pose_or_a_near_door():
    assert 'no judgement' in judge_doorway(None, DOOR, ROBOT)[1]
    assert judge_doorway(_room(), None, ROBOT)[1] == 'no door place'
    assert 'no robot pose' in judge_doorway(_room(), DOOR, None)[1]
    assert 'away' in judge_doorway(_room(), DOOR, (9.0, -2.0, -0.45))[1]


def test_the_tree_can_raise_the_limit():
    points = np.vstack([_room(), _wall(40)])

    assert not judge_doorway(points, DOOR, ROBOT)[0]
    assert judge_doorway(points, DOOR, ROBOT, inputs={'max_points': 50})[0]


def test_move_points_rotates_then_shifts():
    quarter = (0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4))
    moved   = move_points([[1.0, 0.0, 0.0]], (0.5, 0.0, 0.2), quarter)

    assert moved[0] == pytest.approx([0.5, 1.0, 0.2])
