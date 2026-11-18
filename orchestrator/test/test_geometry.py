import math

import pytest

from orchestrator.common.geometry import multiply, rotate, to_rpy, transform_pose, turn_2d, wrap, yaw_quaternion


QUARTER = (0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4))           # 90 deg about z


def test_rotate_and_transform_a_pose_into_another_frame():
    assert rotate(QUARTER, (1.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0))

    position, rotation = transform_pose((1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (0.5, 0.0, 0.2), QUARTER)

    assert position == pytest.approx((0.5, 1.0, 0.2))
    assert rotate(rotation, (1.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0))


def test_quaternions_compose_and_read_back_as_yaw():
    assert multiply(QUARTER, QUARTER) == pytest.approx(yaw_quaternion(math.pi))
    assert to_rpy(yaw_quaternion(0.3)) == pytest.approx((0.0, 0.0, 0.3))
    assert yaw_quaternion(math.pi / 2) == pytest.approx(QUARTER)


def test_planar_steps_and_angles():
    assert turn_2d(math.pi / 2, 0.2, 0.1) == pytest.approx((-0.1, 0.2))         # forward is +y, left is -x
    assert wrap(3 * math.pi / 2) == pytest.approx(-math.pi / 2)
