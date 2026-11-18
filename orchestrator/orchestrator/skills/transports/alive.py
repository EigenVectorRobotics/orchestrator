from skill_registry import Contract

from ...health.checks import judge_alive, run_command, url_answers
from ..api import FutureHandle
from ..context import SkillContext
from .base import Transport


class AliveTransport(Transport):
    """Each listed topic has a publisher, each service exists, each URL answers"""

    name = 'alive'

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> FutureHandle:
        return FutureHandle(ctx.submit('commands', check_alive, ctx.node, contract.interface.alive))


class CommandTransport(Transport):
    """The contract's fixed shell command on the executor's machine; exit code 0 is success"""

    name = 'command'

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> FutureHandle:
        return FutureHandle(ctx.submit('commands', run_command, contract.interface.command, contract.timeout_s or 60.0))


def check_alive(node, spec: dict) -> tuple[bool, str, dict]:
    published = {name for name, _ in node.get_topic_names_and_types() if node.count_publishers(name) > 0}
    services  = {name for name, _ in node.get_service_names_and_types()}

    return judge_alive(spec, published, services, url_answers)
