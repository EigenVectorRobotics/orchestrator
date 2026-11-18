from skill_registry import Contract, Registry

from .api import DoneHandle, Handle, Skill
from .context import SkillContext
from .loader import SkillLibrary
from .transports import Transport, default_transports


CALL_ERRORS = (AttributeError, ValueError, TypeError, KeyError, ModuleNotFoundError)


class SkillRunner:
    """Starts every call of a non-agent skill: the skill's own before, start and wrap around its transport"""

    def __init__(self, ctx: SkillContext, registry: Registry, library: SkillLibrary | None = None,
                 transports: dict[str, Transport] | None = None):
        self.ctx        = ctx
        self.library    = library or SkillLibrary()
        self.transports = transports or default_transports()
        self.skills: dict[str, Skill] = {}

        for contract in registry:
            self.prepare(contract)

    def prepare(self, contract: Contract):
        """Make clients, subscriptions and the skill's own state early, so a tree's first call finds them ready"""
        interface = contract.interface
        transport = self.transports.get(interface.transport)

        try:
            if interface.success_when is not None:
                self.ctx.watch(interface.success_when)

            if transport is not None:
                transport.prepare(contract, self.ctx)

            skill_class = self.library.find(contract.name, 'skill', Skill)

            if skill_class is not None:
                self.skills[contract.name] = skill_class(contract, self.ctx)
                self.skills[contract.name].setup()
        except (*CALL_ERRORS, ImportError) as error:
            self.ctx.logger.warn(f'{contract.name}: {error}; it fails if a tree uses it')

    def start(self, contract: Contract, inputs: dict) -> Handle:
        skill     = self.skills.get(contract.name)
        transport = self.transports.get(contract.interface.transport)

        try:
            if skill is not None and (refused := skill.before(inputs)) is not None:
                return refused

            handle = skill.start(inputs) if skill is not None else None

            if handle is None and transport is None:
                return DoneHandle(False, f'transport {contract.interface.transport} is not supported by this runner')

            handle = handle if handle is not None else transport.start(contract, inputs, self.ctx)

            return skill.wrap(handle, inputs) if skill is not None else handle
        except CALL_ERRORS as error:
            return DoneHandle(False, f'cannot call {contract.name}: {error}')

    def shutdown(self):
        for skill in self.skills.values():
            skill.shutdown()

        self.ctx.shutdown()
