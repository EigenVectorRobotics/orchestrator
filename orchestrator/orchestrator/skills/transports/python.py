from skill_registry import Contract

from ..api import DoneHandle
from ..context import SkillContext
from .base import Transport


class PythonTransport(Transport):
    """A skill whose skill.py runs every call itself; reaching here means it did not"""

    name = 'python'

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> DoneHandle:
        return DoneHandle(False, f'{contract.name}: transport python needs a skill.py whose start() runs the call')
