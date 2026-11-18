import copy

from rclpy.node import Node
from geometry_msgs.msg import PoseStamped
from rosidl_runtime_py import set_message_fields

from skill_registry import Contract
from skill_registry.contract import WHOLE_MESSAGE

from ..common.geometry import yaw_quaternion


def to_pose_stamped(pose: dict, node: Node) -> PoseStamped:
    message = PoseStamped()
    yaw     = float(pose.get('yaw', 0.0))

    message.header.frame_id     = pose.get('frame', 'map')
    message.header.stamp        = node.get_clock().now().to_msg()
    message.pose.position.x     = float(pose['x'])
    message.pose.position.y     = float(pose['y'])
    message.pose.position.z     = float(pose.get('z', 0.0))     # the storey on a multi-storey map; 0 = the lowest
    _, _, message.pose.orientation.z, message.pose.orientation.w = yaw_quaternion(yaw)

    return message


def build_message(message_type, contract: Contract, inputs: dict, node: Node):
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


def extract_outputs(contract: Contract, result) -> dict:
    return {port.name: getattr(result, port.message_field, None) for port in contract.outputs}


def read_field(message, dotted: str):
    for name in dotted.split('.'):
        message = message.get(name) if isinstance(message, dict) else getattr(message, name, None)

    return message
