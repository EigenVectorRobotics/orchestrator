"""A contract against its real ROS types: type names, message fields, fixed fields and outputs"""

from types import SimpleNamespace

from builtin_interfaces.msg import Time
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from .contract import Contract
from .geometry import pose_dict
from .ros_messages import build_message, read_field


SAMPLES = {'string': 'x', 'int': 0, 'float': 0.0, 'bool': False, 'pose': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}
NODE    = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=Time)))   # all build_message needs


def _has_field(message, dotted: str) -> bool:
    for name in dotted.split('.'):
        if name not in message.get_fields_and_field_types():
            return False

        message = getattr(message, name)

    return True


def _not_a_pose(ros_type: str, port) -> str:
    return (f'{ros_type} field {port.message_field} is not a pose, so the port cannot be type pose: a pose field '
            f'has position and orientation, or it holds one')


def _is_pose(value) -> bool:
    """What pose_dict can read, asked by reading it, so the refusal at write time and the conversion at run time
    cannot disagree: a Pose, or anything that wraps one (PoseStamped, PoseWithCovariance, ...)"""
    try:
        pose_dict(value)
    except AttributeError:
        return False

    return True


def check_ros_types(contract: Contract) -> list[str]:
    """What does not match; raises ModuleNotFoundError when an interface package is not built here"""
    interface = contract.interface
    watch     = interface.success_when
    errors    = []

    if watch is not None and not watch.json and not _has_field(get_message(watch.ros_type)(), watch.field):
        errors.append(f'{watch.ros_type} has no field {watch.field}')

    if interface.transport == 'watch' and watch is not None and watch.json:
        errors += [f'the output {port.name} cannot be type pose: a json watch gives text, whose shape cannot be '
                   f'checked here; read the numbers as float ports' for port in contract.outputs if port.type == 'pose']

    if interface.transport == 'watch' and watch is not None and not watch.json:
        message = get_message(watch.ros_type)()

        for port in contract.outputs:
            if not _has_field(message, port.message_field):
                errors.append(f'{watch.ros_type} has no field {port.message_field}')
            elif port.type == 'pose' and not _is_pose(read_field(message, port.message_field)):
                errors.append(_not_a_pose(watch.ros_type, port))

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
    flat    = {} if result is None else result.get_fields_and_field_types()
    errors += [f'{interface.ros_type} result has no field {port.message_field} (an output is a top-level field)'
               for port in contract.outputs if port.message_field not in flat]

    # port_output shapes a pose output of any transport, so the check belongs to every transport too
    errors += [_not_a_pose(interface.ros_type, port) for port in contract.outputs
               if port.type == 'pose' and port.message_field in flat and not _is_pose(getattr(result(), port.message_field))]

    return errors
