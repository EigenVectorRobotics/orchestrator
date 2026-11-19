"""A contract's ports to and from ROS messages: the goal, request or topic message a call sends, and the outputs it
reads back. The runner uses it at run time, and contract_check at write time, so both agree"""

import copy

from rosidl_runtime_py import set_message_fields

from .contract import WHOLE_MESSAGE, Contract
from .geometry import frame_of, pose_dict, to_pose_stamped


def build_message(message_type, contract: Contract, inputs: dict, node):
    """A goal, request or topic message with the fixed fields, then the input ports"""
    message = message_type()

    if contract.interface.fixed:      # a copy: set_message_fields turns a list of dicts into messages in place
        set_message_fields(message, copy.deepcopy(contract.interface.fixed))

    for port in contract.inputs:
        if port.name not in inputs:
            continue

        value = inputs[port.name]

        if port.type == 'pose':
            value = to_pose_stamped(value, node)
        elif port.type == 'enum' and port.values:
            value = port.values[value]
        elif port.type == 'float':
            value = float(value)

        if port.message_field == WHOLE_MESSAGE:
            message = value
            continue

        *parents, leaf = port.message_field.split('.')
        target          = message

        for parent in parents:             # a number is an index into a sequence, e.g. parameters.0.value
            target = target[int(parent)] if parent.isdigit() else getattr(target, parent)

        setattr(target, leaf, value)

    return message


def port_output(value, port, message):
    """What a port hands over, for EVERY transport: a pose port gives the pose shape, never the ROS message, so the
    blackboard, a {key} input, the trace and the code sandbox all read {x, y, z, yaw, frame}. Another port keeps
    what the field holds (ground's detections are a message that the next skill reads by its key). The frame comes
    from the message the field was read from"""
    if port.type == 'pose' and hasattr(value, 'get_fields_and_field_types'):
        return pose_dict(value, frame_of(message))

    return value


def extract_outputs(contract: Contract, result) -> dict:
    return {port.name: port_output(getattr(result, port.message_field, None), port, result)
            for port in contract.outputs}


def read_field(message, dotted: str):
    for name in dotted.split('.'):
        message = message.get(name) if isinstance(message, dict) else getattr(message, name, None)

    return message
