import math

import pytest

from orchestrator.stance_checks import judge_reach, judge_view, rotate, transform_pose


IMAGE = (1280, 720)


# ========== In view ==========

def test_a_fresh_box_away_from_the_edges_is_in_view():
    success, message, outputs = judge_view((600, 500, 660, 580), 0.2, IMAGE)

    assert success and 'in view' in message
    assert outputs['edge_px']['bottom'] == pytest.approx(140)


def test_a_box_cut_by_the_bottom_edge_is_not_in_view():
    """E-015: the robot stopped too close and the button fell below the frame"""
    success, message, outputs = judge_view((600, 690, 660, 720), 0.2, IMAGE)

    assert not success and 'bottom 0 px' in message
    assert outputs['box'] == [600, 690, 660, 720]


def test_no_box_or_an_old_one_means_not_tracking():
    assert not judge_view(None, None, IMAGE)[0]
    assert 'last 3.0 s ago' in judge_view((600, 500, 660, 580), 3.0, IMAGE)[1]
    assert judge_view((600, 500, 660, 580), 0.2, None)[1] == 'no camera image yet'


def test_the_tree_can_narrow_the_margin():
    box = (600, 650, 660, 700)                      # 20 px from the bottom: inside 5 % (36 px), outside 2 % (14 px)

    assert not judge_view(box, 0.2, IMAGE)[0]
    assert judge_view(box, 0.2, IMAGE, inputs={'margin_frac': 0.02})[0]


# ========== In reach ==========

def test_a_button_in_the_range_that_worked_is_in_reach():
    success, message, outputs = judge_reach((0.40, -0.05, 0.22), (1.0, 0.0, 0.0), 0.1)

    assert success and 'in the reach that worked' in message
    assert outputs['ahead_m'] == pytest.approx(0.40) and outputs['slant_deg'] == pytest.approx(0.0)


@pytest.mark.parametrize('position, normal, problem', [
    ((0.55, 0.0, 0.2), (1.0, 0.0, 0.0), 'too far: 0.550'),                 # E-018: 0.55 m from floor 1
    ((0.25, 0.0, 0.2), (1.0, 0.0, 0.0), 'too close: 0.250'),
    ((0.40, -0.20, 0.2), (1.0, 0.0, 0.0), '0.200 m to the right'),
    ((0.40, 0.0, 0.2), (math.cos(math.radians(60)), math.sin(math.radians(60)), 0.0), 'at 60 deg'),
])
def test_a_stance_outside_the_range_fails_with_what_is_wrong(position, normal, problem):
    success, message, _ = judge_reach(position, normal, 0.1)

    assert not success and problem in message


def test_no_fresh_pose_points_at_no_track_or_no_depth():
    """E-005: closer than about 0.28 m the D435 gives no depth, so there is no 3D pose"""
    success, message, outputs = judge_reach(None, None, None)

    assert not success and 'too close for depth' in message and outputs == {}
    assert not judge_reach((0.4, 0.0, 0.2), (1.0, 0.0, 0.0), 2.5)[0]


def test_a_panel_range_given_by_the_tree_overrides_the_contract():
    """E-006: 0.48 m worked outside; inside, 0.46 m is the far end"""
    limits = {'min_ahead_m': 0.33, 'max_ahead_m': 0.48}

    assert judge_reach((0.47, 0.0, 0.2), (1.0, 0.0, 0.0), 0.1, limits)[0]
    assert not judge_reach((0.47, 0.0, 0.2), (1.0, 0.0, 0.0), 0.1, limits, {'min_ahead_m': 0.38, 'max_ahead_m': 0.46})[0]
    assert judge_reach((0.47, 0.0, 0.2), (1.0, 0.0, 0.0), 0.1, limits, {'max_ahead_m': None})[0]     # unset input


# ========== Poses ==========

def test_rotate_and_transform_a_pose_into_another_frame():
    quarter = (0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4))           # 90 deg about z

    assert rotate(quarter, (1.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0))

    position, rotation = transform_pose((1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (0.5, 0.0, 0.2), quarter)

    assert position == pytest.approx((0.5, 1.0, 0.2))
    assert rotate(rotation, (1.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0))
