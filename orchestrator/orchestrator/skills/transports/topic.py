from rosidl_runtime_py.utilities import get_message

from skill_registry import Contract

from ..context import SkillContext
from ..handles import TopicHandle
from ..messages import build_message
from .base import Transport


class TopicTransport(Transport):
    name = 'topic'

    def prepare(self, contract: Contract, ctx: SkillContext):
        ctx.publisher(contract.interface.name, contract.interface.ros_type)

        if contract.interface.success_when is not None:
            ctx.watch(contract.interface.success_when)

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> TopicHandle:
        interface = contract.interface
        message   = build_message(get_message(interface.ros_type), contract, inputs, ctx.node)
        watch     = ctx.watch(interface.success_when) if interface.success_when else None

        return TopicHandle(ctx.publisher(interface.name, interface.ros_type), message, contract, watch, ctx.server_wait_s)
