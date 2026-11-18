from skill_registry import Contract

from ..api import Handle
from ..context import SkillContext


class Transport:
    """How the executor reaches what a contract names: one instance per transport, shared by every skill using it"""

    name = ''

    def prepare(self, contract: Contract, ctx: SkillContext):
        """At startup: make clients early, so discovery is done when a tree first asks"""

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> Handle:
        raise NotImplementedError
