"""A contract against its real ROS types: type names, message fields, fixed fields and outputs"""

from types import SimpleNamespace

from builtin_interfaces.msg import Time
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from skill_registry import Contract

from .ros_skill_runner import build_message


SAMPLES = {'string': 'x', 'int': 0, 'float': 0.0, 'bool': False, 'pose': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}
NODE    = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=Time)))   # all build_message needs


def _has_field(message, dotted: str) -> bool:
    for name in dotted.split('.'):
        if name not in message.get_fields_and_field_types():
            return False

        message = getattr(message, name)

    return True


def check_ros_types(contract: Contract) -> list[str]:
    """What does not match; raises ModuleNotFoundError when an interface package is not built here"""
    interface = contract.interface
    watch     = interface.success_when
    errors    = []

    if watch is not None and not watch.json and not _has_field(get_message(watch.ros_type)(), watch.field):
        errors.append(f'{watch.ros_type} has no field {watch.field}')

    if interface.transport == 'watch' and watch is not None and not watch.json:
        message = get_message(watch.ros_type)()
        errors += [f'{watch.ros_type} has no field {port.message_field}' for port in contract.outputs
                   if not _has_field(message, port.message_field)]

    if interface.transport == 'action':
        action_type      = get_action(interface.ros_type)
        outgoing, result = action_type.Goal, action_type.Result

        if interface.failure_when is not None and not _has_field(action_type.Feedback(), interface.failure_when.field):
            errors.append(f'{interface.ros_type} feedback has no field {interface.failure_when.field}')
    elif interface.transport == 'service':
        service_type     = get_service(interface.ros_type)
        outgoing, result = service_type.Request, service_type.Response
    elif interface.transport == 'topic':
        outgoing, result = get_message(interface.ros_type), None
    else:
        return errors

    try:
        sample = {port.name: port.choices[0] if port.type == 'enum' else SAMPLES[port.type] for port in contract.inputs}
        build_message(outgoing, contract, sample, NODE)
    except (AttributeError, TypeError, ValueError, KeyError, IndexError, AssertionError) as error:
        errors.append(f'the inputs do not fit {interface.ros_type}: {error}')

    # outputs are read flat (ros_skill_runner: getattr(result, field)), so a dotted path would always give None
    errors += [f'{interface.ros_type} result has no field {port.message_field} (an output is a top-level field)'
               for port in contract.outputs if result is None or port.message_field not in result.get_fields_and_field_types()]

    return errors
