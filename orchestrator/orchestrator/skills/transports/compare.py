from skill_registry import Contract

from ..api import DoneHandle
from ..context import SkillContext
from .base import Transport


class CompareTransport(Transport):
    """The input value equals the input equals; nothing leaves the executor"""

    name = 'compare'

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> DoneHandle:
        value, equals = str(inputs['value']), str(inputs['equals'])

        return DoneHandle(value == equals, f'{value} {"==" if value == equals else "!="} {equals}')
