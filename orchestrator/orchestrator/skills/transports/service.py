from rosidl_runtime_py.utilities import get_service

from skill_registry import Contract

from ..context import SkillContext
from ..handles import ServiceHandle
from ..messages import build_message
from .base import Transport


class ServiceTransport(Transport):
    name = 'service'

    def prepare(self, contract: Contract, ctx: SkillContext):
        ctx.service_client(contract.interface.name, get_service(contract.interface.ros_type))

        if contract.interface.success_when is not None:
            ctx.watch(contract.interface.success_when)

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> ServiceHandle:
        interface    = contract.interface
        service_type = get_service(interface.ros_type)
        request      = build_message(service_type.Request, contract, inputs, ctx.node)
        watch        = ctx.watch(interface.success_when) if interface.success_when else None

        return ServiceHandle(ctx.service_client(interface.name, service_type), request, contract, ctx.server_wait_s, watch)
