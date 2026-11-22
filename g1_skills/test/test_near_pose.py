"""near_pose: before each look's walk, whether the robot already stands on the goal, so the walk would take no step."""

import math

from types import SimpleNamespace

import pytest

from skill_registry import parse_contract

from orchestrator.tree.engine import Status

from g1_skills.lib.walk import APPROACH_BACK_M, WALK_MIN_M
from g1_skills.near_pose.skill import NearPose, near


GOAL     = {'x': 3.389, 'y': 0.303, 'z': -0.371, 'yaw': 2.6459, 'frame': 'map'}    # the inside panel of run 006
CONTRACT = parse_contract({
    'name': 'near_pose', 'kind': 'condition', 'device': 'nuc', 'timeout_s': 3,
    'interface': {'transport': 'python', 'name': '/tf', 'ros_type': 'tf2_msgs/msg/TFMessage'},
    'inputs': [{'name': 'goal', 'type': 'pose'}],
})


class FakeTf:
    def __init__(self, xy=None):
        self.xy = xy

    def lookup(self, target_frame, source_frame, timeout_s):
        if self.xy is None:
            raise RuntimeError('no transform')
        return self.xy

    def numbers(self, xy):
        return {'translation_m': [xy[0], xy[1], 0.0]}


def _run(xy):
    handle = NearPose(CONTRACT, SimpleNamespace(tf=FakeTf(xy))).start({'goal': GOAL})
    return handle.status == Status.SUCCESS, handle.outputs


def test_on_the_goal_is_near_and_walks_out_behind_it():
    success, outputs = _run((3.410, 0.260))         # where run 006 stood: 4.8 cm off
    back             = math.dist((outputs['approach']['x'], outputs['approach']['y']), (GOAL['x'], GOAL['y']))

    assert success and outputs['distance_m'] < WALK_MIN_M
    assert back == pytest.approx(APPROACH_BACK_M)
    assert outputs['approach']['yaw'] == pytest.approx(GOAL['yaw']) and outputs['approach']['z'] == GOAL['z']
    # behind: against the goal's heading
    heading = (math.cos(GOAL['yaw']), math.sin(GOAL['yaw']))
    assert (outputs['approach']['x'] - GOAL['x']) * heading[0] + (outputs['approach']['y'] - GOAL['y']) * heading[1] < 0


def test_far_from_the_goal_walks_straight():
    success, outputs = _run((3.389 + 0.19 * math.cos(2.6459 + math.pi), 0.303 + 0.19 * math.sin(2.6459 + math.pi)))

    assert not success and outputs['distance_m'] == pytest.approx(0.19, abs=1e-3)


def test_no_transform_walks_straight():
    success, outputs = _run(None)

    assert not success and outputs == {}


def test_near_is_strictly_under_the_walk_minimum():
    assert near({'x': GOAL['x'] + WALK_MIN_M - 1e-3, 'y': GOAL['y']}, GOAL)[0]
    assert not near({'x': GOAL['x'] + WALK_MIN_M + 1e-3, 'y': GOAL['y']}, GOAL)[0]
