from rosidl_runtime_py.utilities import get_action

from skill_registry import Contract

from ..context import SkillContext
from ..handles import ActionHandle
from ..messages import build_message
from .base import Transport


class ActionTransport(Transport):
    name = 'action'

    def prepare(self, contract: Contract, ctx: SkillContext):
        ctx.action_client(contract.interface.name, get_action(contract.interface.ros_type))

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> ActionHandle:
        action_type = get_action(contract.interface.ros_type)
        goal        = build_message(action_type.Goal, contract, inputs, ctx.node)

        return ActionHandle(ctx.action_client(contract.interface.name, action_type), goal, contract, ctx.server_wait_s)
