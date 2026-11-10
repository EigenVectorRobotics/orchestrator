"""The test contracts against their real ROS types: type names, message fields, fixed fields and outputs.

check_ros_types is what the executor runs on every contract the agent writes (write_skill), so it is tested here
against the copies in skill_registry/test/data. Needs a sourced ROS 2 workspace; a contract whose interface package
is not built here is skipped, not failed.
"""

import pytest

pytest.importorskip('rosidl_runtime_py')

from skill_registry import Registry

from orchestrator.contract_check import check_ros_types

from data_paths import CONTRACTS


REGISTRY = Registry.from_directory(CONTRACTS)


@pytest.mark.parametrize('contract', list(REGISTRY), ids=lambda contract: contract.name)
def test_contract_matches_its_ros_types(contract):
    try:
        assert check_ros_types(contract) == []
    except ModuleNotFoundError as error:
        pytest.skip(f'{contract.interface.ros_type}: {error.name} is not built here')
