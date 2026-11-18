"""Every contract against its real ROS types: type names, message fields, fixed fields and outputs.

Needs a sourced ROS 2 workspace. A contract whose interface package is not built here is skipped, not failed.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('rosidl_runtime_py')

from builtin_interfaces.msg import Time
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from skill_registry import Registry

from orchestrator.skills.transports import build_message


CONTRACTS = Path(__file__).resolve().parents[1] / 'g1_skills'
REGISTRY  = Registry.from_directory(CONTRACTS)
SAMPLES   = {'string': 'x', 'int': 0, 'float': 0.0, 'bool': False, 'pose': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}
NODE      = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=Time)))   # all build_message needs


def _load(loader, ros_type: str):
    try:
        return loader(ros_type)
    except ModuleNotFoundError as error:
        pytest.skip(f'{ros_type}: {error.name} is not built here')


def _sample(port):
    return port.choices[0] if port.type == 'enum' else SAMPLES[port.type]


def _has_field(message, dotted: str) -> bool:
    for name in dotted.split('.'):
        if name not in message.get_fields_and_field_types():
            return False

        message = getattr(message, name)

    return True


@pytest.mark.parametrize('contract', list(REGISTRY), ids=lambda contract: contract.name)
def test_contract_matches_its_ros_types(contract):
    interface = contract.interface
    watch     = interface.success_when

    if watch is not None and not watch.json:
        assert _has_field(_load(get_message, watch.ros_type)(), watch.field), f'{watch.ros_type} has no field {watch.field}'

    if interface.transport == 'action':
        action_type = _load(get_action, interface.ros_type)
        outgoing, result = action_type.Goal, action_type.Result

        if interface.failure_when is not None:
            assert _has_field(action_type.Feedback(), interface.failure_when.field), f'{interface.ros_type} feedback has no field {interface.failure_when.field}'
    elif interface.transport == 'service':
        service_type = _load(get_service, interface.ros_type)
        outgoing, result = service_type.Request, service_type.Response
    elif interface.transport == 'topic':
        outgoing, result = _load(get_message, interface.ros_type), None
    else:
        return

    build_message(outgoing, contract, {port.name: _sample(port) for port in contract.inputs}, NODE)

    for port in contract.outputs:
        assert result is not None and _has_field(result(), port.message_field), f'{interface.ros_type} result has no field {port.message_field}'
