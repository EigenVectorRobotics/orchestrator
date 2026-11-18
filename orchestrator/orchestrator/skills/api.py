from typing import TYPE_CHECKING, Callable

from skill_registry import Contract

from ..tree.engine import Status

if TYPE_CHECKING:
    from .context import SkillContext


class Handle:
    """One running call of a skill, polled by its leaf every tick until it is done"""

    def __init__(self):
        self.status  = Status.RUNNING
        self.message = ''
        self.outputs = {}
        self.on_end: list[Callable[['Handle'], None]] = []     # called once, when the call ends

    def poll(self) -> Status:
        return self.status

    def cancel(self):
        pass

    def _finish(self, success: bool, message: str = ''):
        if self.status != Status.RUNNING:
            return                                      # already ended; a late result changes nothing

        self.status  = Status.SUCCESS if success else Status.FAILURE
        self.message = message

        for listener in self.on_end:
            listener(self)


class DoneHandle(Handle):
    """A call that ended before it started"""

    def __init__(self, success: bool, message: str = '', outputs: dict | None = None):
        super().__init__()

        self._finish(success, message)
        self.outputs = outputs or {}


class FutureHandle(Handle):
    """Work run in a thread: the future gives (success, message, outputs)"""

    def __init__(self, future):
        super().__init__()

        self.future = future

    def poll(self) -> Status:
        if self.status != Status.RUNNING or not self.future.done():
            return self.status

        try:
            success, message, self.outputs = self.future.result()
        except Exception as error:                      # a crash in the thread is a failed skill, not a dead executor
            success, message = False, f'{type(error).__name__}: {error}'

        self._finish(success, message)

        return self.status


class Skill:
    """The custom logic of one skill: its folder's skill.py defines one subclass, and every method is optional.

    A call runs before(), then start() (the contract's transport when start returns None), then wrap() on its handle.
    Transport python has no default: its skill must start every call.
    """

    def __init__(self, contract: Contract, ctx: 'SkillContext'):
        self.contract = contract
        self.ctx      = ctx
        self.config   = contract.config

    def setup(self):
        """Once at startup: subscriptions, timers, shared state"""

    def before(self, inputs: dict) -> Handle | None:
        """A handle refuses the call without sending anything; None lets it go on"""
        return None

    def start(self, inputs: dict) -> Handle | None:
        """The whole call; None leaves it to the transport"""
        return None

    def wrap(self, handle: Handle, inputs: dict) -> Handle:
        """Watch or replace the running call's handle"""
        return handle

    def shutdown(self):
        pass
