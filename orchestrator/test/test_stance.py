import math

import pytest

from orchestrator import stance


BASE = {'x': 1.829, 'y': -1.372, 'yaw': -0.272, 'frame': 'map'}


def _tf(translation, yaw: float) -> dict:
    return {'translation_m': list(translation), 'rotation_xyzw': [0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)],
            'rpy_rad': [0.0, 0.0, yaw]}


def test_relative_pose_is_in_the_base_goal_frame():
    robot = {'x': BASE['x'] + 0.1 * math.cos(BASE['yaw']), 'y': BASE['y'] + 0.1 * math.sin(BASE['yaw']), 'yaw': BASE['yaw'] + 0.1}

    assert stance.relative_pose(robot, BASE) == pytest.approx({'forward_m': 0.1, 'left_m': 0.0, 'yaw_rad': 0.1}, abs=1e-4)


def test_transform_point_rotates_then_translates():
    assert stance.transform_point(_tf((1.0, 2.0, 0.0), math.pi / 2), [1.0, 0.0, 0.5]) == pytest.approx([1.0, 3.0, 0.5])


def test_match_steps_the_robot_by_the_target_offset_from_the_reference():
    # the button sits 1.5 cm further and 5.4 cm further left than in the press that worked: step forward and left
    found = stance.match({'forward_m': -0.043, 'left_m': -0.054, 'yaw_rad': 0.0}, [0.415, -0.050, 0.25], [0.400, -0.104, 0.25])

    assert (found['dx'], found['dy']) == pytest.approx((-0.028, 0.0), abs=1e-3)
    assert found['move_m'] == pytest.approx(0.056, abs=1e-3) and not found['walks']


def test_the_reference_is_the_latest_for_the_button_near_the_target():
    references = [
        {'button': 'down button', 'target_map': [2.2, -1.5, 0.6], 'n': 1},
        {'button': 'down button', 'target_map': [9.0, 4.0, -6.4], 'n': 2},       # the other floor's panel
        {'button': 'floor 1', 'target_map': [2.2, -1.5, 0.6], 'n': 3},
        {'button': 'down button', 'target_map': [2.25, -1.45, 0.6], 'n': 4},
    ]

    assert stance.nearest_reference(references, 'down button', [2.2, -1.5, 0.6], None)['n'] == 4
    assert stance.nearest_reference(references, 'down button', None, {'x': 1.83, 'y': -1.37})['n'] == 4
    assert stance.nearest_reference(references, 'up button', [2.2, -1.5, 0.6], None) is None


def test_last_goal_reads_the_checked_press():
    pose    = {'header': {'frame_id': 'foot_world'}, 'pose': {'position': {'x': 0.4, 'y': -0.1, 'z': 1.0}}}
    failure = {'skill': 'press_judge', 'checked_call': {'feedback': [{'t': 0.1}, {'t': 0.2, 'current_goal': pose}]}}

    assert stance.last_goal(failure) == [0.4, -0.1, 1.0]
    assert stance.last_goal({'skill': 'button_in_view', 'feedback': []}) is None


def test_evidence_says_when_the_match_does_not_walk(tmp_path):
    path = str(tmp_path / 'references.jsonl')
    stance.save_reference({'button': 'down button', 'left_arm': False, 'robot': {'x': 1.83, 'y': -1.37, 'yaw': -0.27},
                           'target_pelvis': [0.400, -0.104, 0.25], 'target_map': [2.2, -1.5, 0.6]}, path)
    robot  = {'x': BASE['x'] - 0.043 * math.cos(BASE['yaw']), 'y': BASE['y'] - 0.043 * math.sin(BASE['yaw']), 'yaw': BASE['yaw']}
    found  = stance.evidence(BASE, robot, [0.415, -0.050, 0.25], [2.21, -1.49, 0.6], False, 'down button', stance.load_references(path))

    assert found['reference']['target_in_pelvis'] == [0.400, -0.104, 0.25]
    assert found['match']['walks'] is False and 'does not walk' in found['note']


def test_the_other_arm_reference_is_reported_with_its_distance_to_the_target():
    references = [
        {'time': 1, 'button': 'down button', 'left_arm': False, 'robot': {'x': 1.83, 'y': -1.37, 'yaw': -0.27},
         'target_pelvis': [0.400, -0.104, 0.25], 'target_map': [2.2, -1.5, 0.6]},
        {'time': 2, 'button': 'down button', 'left_arm': True, 'robot': {'x': 1.83, 'y': -1.37, 'yaw': -0.27},
         'target_pelvis': [0.410, -0.060, 0.25], 'target_map': [2.21, -1.49, 0.6]},
    ]
    found = stance.evidence(BASE, {'x': 1.83, 'y': -1.37, 'yaw': -0.27}, [0.415, -0.050, 0.25], [2.21, -1.49, 0.6], False,
                            'down button', references)

    assert found['reference']['left_arm'] is False and found['other_arm']['left_arm'] is True
    assert found['other_arm']['target_offset_m'] == pytest.approx(0.011, abs=1e-3)


def test_the_walk_back_to_the_base_goal_needs_no_reference():
    robot = {'x': BASE['x'] - 0.10 * math.cos(BASE['yaw']) + 0.087 * math.sin(BASE['yaw']),
             'y': BASE['y'] - 0.10 * math.sin(BASE['yaw']) - 0.087 * math.cos(BASE['yaw']), 'yaw': BASE['yaw']}
    found = stance.evidence(BASE, robot, [0.633, 0.05, 0.22], [3.9, -0.3, 0.6], False, 'floor 1', [])

    assert found['reference'] is None and found['match'] is None
    assert found['to_base_goal'] == {'dx': 0.0, 'dy': 0.0, 'dyaw': 0.0, 'move_m': pytest.approx(0.133, abs=1e-3), 'walks': True}


def test_latest_reach_gives_the_last_check_reach_miss_on_three_axes():
    check = {'skill': 'check_reach', 'status': 'failure', 'inputs': {'left_arm': True},
             'outputs': {'position_error': 0.307, 'orientation_error_deg': 7.9,
                         'pelvis_error_x': -0.296, 'pelvis_error_y': 0.005, 'pelvis_error_z': 0.082,
                         'target_pelvis_x': 0.7, 'target_pelvis_y': 0.1, 'target_pelvis_z': 0.2}}
    press = {'skill': 'press_button', 'status': 'failure', 'outputs': {}}

    found = stance.latest_reach(press, [{'skill': 'navigate_to'}, check, press])

    assert found['reachable'] is False and found['left_arm'] is True
    assert found['pelvis_error_m'] == {'x': -0.296, 'y': 0.005, 'z': 0.082}
    assert found['target_in_pelvis'] == {'x': 0.7, 'y': 0.1, 'z': 0.2}
    assert stance.latest_reach(check, [])['position_error_m'] == 0.307
    assert stance.latest_reach(press, [{'skill': 'navigate_to'}]) is None


def test_a_reach_check_before_a_walk_is_stale():
    check = {'skill': 'check_reach', 'status': 'success', 'outputs': {'pelvis_error_x': 0.0, 'pelvis_error_y': 0.0, 'pelvis_error_z': 0.0}}

    assert stance.latest_reach(None, [check, {'skill': 'navigate_to'}, {'skill': 'button_in_view'}]) is None
    assert stance.latest_reach(None, [check, {'skill': 'button_in_view'}])['reachable'] is True


def test_reach_move_steps_by_minus_the_miss_plus_margin_in_the_base_goal_frame():
    reach  = {'reachable': False, 'pelvis_error_m': {'x': -0.10, 'y': 0.0, 'z': 0.01}}
    turned = stance.reach_move({'forward_m': 0.0, 'left_m': 0.0, 'yaw_rad': math.pi / 2}, reach, {'forward': 0.15, 'back': 0.6, 'side': 0.25})
    ahead  = stance.reach_move({'forward_m': -0.05, 'left_m': 0.02, 'yaw_rad': 0.0}, reach, {'forward': 0.15, 'back': 0.6, 'side': 0.25})

    assert ahead['fixable'] and ahead['step_pelvis_m'] == [pytest.approx(0.12), pytest.approx(0.0)]
    assert (ahead['dx'], ahead['dy'], ahead['dyaw']) == pytest.approx((0.07, 0.02, 0.0)) and ahead['within_limits']
    assert (turned['dx'], turned['dy']) == pytest.approx((0.0, 0.12)) and turned['dyaw'] == pytest.approx(math.pi / 2, abs=1e-3)


def test_reach_move_leaves_a_vertical_or_orientation_miss_and_a_far_one_to_the_agent():
    limits = {'forward': 0.15, 'back': 0.6, 'side': 0.25}
    here   = {'forward_m': 0.0, 'left_m': 0.0, 'yaw_rad': 0.0}

    assert stance.reach_move(here, {'reachable': False, 'pelvis_error_m': {'x': -0.02, 'y': 0.0, 'z': 0.08}}, limits)['fixable'] is False
    assert stance.reach_move(here, {'reachable': False, 'pelvis_error_m': {'x': 0.001, 'y': 0.0, 'z': 0.0}}, limits)['fixable'] is False
    assert stance.reach_move(here, {'reachable': False, 'pelvis_error_m': {'x': -0.3, 'y': 0.0, 'z': 0.0}}, limits)['within_limits'] is False
    assert stance.reach_move(here, {'reachable': True, 'pelvis_error_m': {'x': 0.0, 'y': 0.0, 'z': 0.0}}, limits) is None
    assert stance.reach_move(here, {'reachable': False, 'pelvis_error_m': None}, limits) is None


def test_evidence_says_how_far_the_press_target_moved_since_the_check():
    reach = {'reachable': True, 'pelvis_error_m': {'x': 0.0, 'y': 0.0, 'z': 0.0}, 'target_in_pelvis': {'x': 0.40, 'y': -0.10, 'z': 0.25}}
    found = stance.evidence(BASE, {'x': BASE['x'], 'y': BASE['y'], 'yaw': BASE['yaw']}, [0.43, -0.06, 0.25], None, False, 'floor 1', [], reach)

    assert found['reach']['target_moved_since_check_m'] == {'xyz': [0.03, 0.04, 0.0], 'norm': 0.05}
    assert found['reach_move'] is None


def test_evidence_carries_the_reach_miss():
    reach = {'reachable': False, 'pelvis_error_m': {'x': -0.1, 'y': 0.0, 'z': 0.0}}
    found = stance.evidence(BASE, {'x': BASE['x'], 'y': BASE['y'], 'yaw': BASE['yaw']}, None, None, False, 'floor 1', [], reach)

    assert found['reach'] is reach
