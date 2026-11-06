"""Every contract against its real ROS types: type names, message fields, fixed fields and outputs.

Needs a sourced ROS 2 workspace. A contract whose interface package is not built here is skipped, not failed.
"""

from pathlib import Path

import pytest

pytest.importorskip('rosidl_runtime_py')

from skill_registry import Registry

from orchestrator.contract_check import check_ros_types


CONTRACTS = Path(__file__).resolve().parents[2] / 'skill_registry' / 'contracts'
REGISTRY  = Registry.from_directory(CONTRACTS)


@pytest.mark.parametrize('contract', list(REGISTRY), ids=lambda contract: contract.name)
def test_contract_matches_its_ros_types(contract):
    try:
        assert check_ros_types(contract) == []
    except ModuleNotFoundError as error:
        pytest.skip(f'{contract.interface.ros_type}: {error.name} is not built here')
