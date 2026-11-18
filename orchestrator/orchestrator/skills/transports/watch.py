import time
import dataclasses

from skill_registry import Contract
from skill_registry.contract import Watch

from ..api import DoneHandle
from ..context import SkillContext
from .base import Transport


class WatchTransport(Transport):
    """A condition on the latest messages of a topic; it never publishes"""

    name = 'watch'

    def prepare(self, contract: Contract, ctx: SkillContext):
        ctx.watch(contract.interface.success_when)

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> DoneHandle:
        return check_watch(contract.interface.success_when, inputs, ctx)


def check_watch(spec: Watch, inputs: dict, ctx: SkillContext) -> DoneHandle:
    if spec.equals_input:
        spec = dataclasses.replace(spec, equals=inputs[spec.equals_input])

    watch = ctx.watch(spec)

    if watch.latest is None:
        return DoneHandle(False, f'no message on {spec.topic} yet')

    age = time.monotonic() - watch.receive_time

    if spec.max_age_s and age > spec.max_age_s:
        return DoneHandle(False, f'no message on {spec.topic} for {age:.1f} s (needs one within {spec.max_age_s:g} s)')

    return DoneHandle(watch.matches(spec), f'{spec.field} = {watch.read(spec)}')
