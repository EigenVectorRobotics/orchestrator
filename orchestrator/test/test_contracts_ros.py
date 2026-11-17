"""The test contracts against their real ROS types: type names, message fields, fixed fields and outputs.

check_ros_types is what the executor runs on every contract the agent writes (write_skill), so it is tested here
against the copies in skill_registry/test/data. Needs a sourced ROS 2 workspace; a contract whose interface package
is not built here is skipped, not failed.
"""

import pytest

pytest.importorskip('rosidl_runtime_py')

from skill_registry import Registry, parse_contract

from orchestrator.contract_check import check_ros_types

from data_paths import CONTRACTS


REGISTRY = Registry.from_directory(CONTRACTS)


@pytest.mark.parametrize('contract', list(REGISTRY), ids=lambda contract: contract.name)
def test_contract_matches_its_ros_types(contract):
    try:
        assert check_ros_types(contract) == []
    except ModuleNotFoundError as error:
        pytest.skip(f'{contract.interface.ros_type}: {error.name} is not built here')


def test_a_watch_reports_fields_of_the_message_it_reads_and_only_those():
    pose_now = {'name': 'pose_now', 'kind': 'condition', 'device': 'nuc',
                'interface': {'transport': 'watch',
                              'success_when': {'topic': '/Odometry', 'ros_type': 'nav_msgs/msg/Odometry',
                                               'field': 'header.frame_id', 'equals': 'map'}},
                'outputs': [{'name': 'x', 'type': 'float', 'field': 'pose.pose.position.x'}]}

    assert check_ros_types(parse_contract(pose_now)) == []

    pose_now['outputs'].append({'name': 'elbow', 'type': 'float', 'field': 'pose.pose.elbow'})

    assert check_ros_types(parse_contract(pose_now)) == ['nav_msgs/msg/Odometry has no field pose.pose.elbow']


def test_a_pose_port_must_name_a_pose_field_whatever_the_transport():
    """port_output shapes a pose output of any transport, so every transport refuses a field that is not a pose"""
    walk = {'name': 'walk', 'kind': 'action', 'device': 'nuc', 'timeout_s': 10.0,
            'interface': {'transport': 'action', 'name': '/navigate_to_goal',
                          'ros_type': 'navigation_interfaces/action/NavigateToGoal'},
            'outputs': [{'name': 'arrived', 'type': 'bool', 'field': 'success'}]}

    assert check_ros_types(parse_contract(walk)) == []

    walk['outputs'] = [{'name': 'arrived', 'type': 'pose', 'field': 'success'}]

    assert 'is not a pose' in check_ros_types(parse_contract(walk))[0]
