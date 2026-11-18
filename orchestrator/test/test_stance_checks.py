
import pytest

from orchestrator.stance_checks import judge_arm, judge_view


IMAGE = (1280, 720)


# ========== In view ==========

def test_a_fresh_box_away_from_the_edges_is_in_view():
    success, message, outputs = judge_view((600, 500, 660, 580), 0.2, IMAGE)

    assert success and 'in view' in message
    assert outputs['edge_px']['bottom'] == pytest.approx(140)


def test_a_box_cut_by_the_bottom_edge_is_not_in_view():
    """The robot stopped too close and the button fell below the frame"""
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


def test_the_left_arm_only_past_the_threshold_to_the_left():
    assert judge_arm((0.42, 0.15, 0.2))[0] is True
    assert judge_arm((0.42, 0.06, 0.2))[0] is False                         # inside: the right arm reached y +0.05 to +0.07
    assert judge_arm((0.42, -0.12, 0.2))[0] is False
    assert judge_arm((0.42, 0.06, 0.2), {'left_arm_min_y_m': 0.05})[0] is True
    assert judge_arm((0.40, 0.04, 0.25), None, {'left_arm_min_y_m': 0.0})[0] is True       # the call panel: by side
    assert judge_arm((0.40, -0.04, 0.25), None, {'left_arm_min_y_m': 0.0})[0] is False
