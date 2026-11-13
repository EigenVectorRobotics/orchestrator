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


def test_reach_match_borrows_the_forward_reach_of_the_same_arm_at_another_button():
    references = [{'time': 1, 'button': 'down button', 'left_arm': False, 'robot': {'x': 1.83, 'y': -1.37, 'yaw': -0.27},
                   'target_pelvis': [0.421, 0.029, 0.275], 'target_map': [2.17, -1.44, -0.26]}]
    found = stance.evidence(BASE, {'x': BASE['x'], 'y': BASE['y'], 'yaw': BASE['yaw']}, [0.646, 0.026, 0.214], [4.0, -0.3, -0.2],
                            False, 'floor 1', references)

    assert found['reference'] is None and found['match'] is None
    assert found['reach_match']['reference_button'] == 'down button'
    assert found['reach_match']['dx'] == pytest.approx(0.225, abs=1e-3) and found['reach_match']['dy'] == pytest.approx(0.0, abs=1e-3)
    assert found['reach_match']['walks'] is True


def test_reach_match_takes_the_other_arm_when_this_arm_has_no_press():
    references = [{'time': 1, 'button': 'down button', 'left_arm': True, 'robot': {'x': 1.83, 'y': -1.37, 'yaw': -0.27},
                   'target_pelvis': [0.421, 0.029, 0.275], 'target_map': [2.17, -1.44, -0.26]}]
    found = stance.evidence(BASE, {'x': BASE['x'], 'y': BASE['y'], 'yaw': BASE['yaw']}, [0.551, -0.116, 0.216], [4.0, -0.3, -0.2],
                            False, 'floor 1', references)

    assert found['reach_match']['reference_left_arm'] is True
    assert found['reach_match']['dx'] == pytest.approx(0.130, abs=1e-3) and found['reach_match']['walks'] is True
