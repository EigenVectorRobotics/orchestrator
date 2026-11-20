
import pytest

from g1_skills.button_in_view.skill import judge_arm, judge_view


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


def test_the_left_arm_only_in_the_left_part_of_the_image():
    assert judge_arm(0.30)[0] is True
    assert judge_arm(0.40)[0] is False                                      # the right 60 % of the image takes the right arm
    assert judge_arm(0.56)[0] is False                                      # the down button straight ahead, 2026-11-19
    assert judge_arm(0.45, {'left_arm_max_x_frac': 0.5})[0] is True
    assert judge_arm(0.45, None, {'left_arm_max_x_frac': 0.3})[0] is False
