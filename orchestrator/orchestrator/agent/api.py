from types import ModuleType
from typing import TYPE_CHECKING
from dataclasses import dataclass

from skill_registry import Contract

from ..tree.engine import Context, Status

if TYPE_CHECKING:
    from ..skills.context import SkillContext
    from .engine import AgentSession


@dataclass
class ToolOutput:
    text:       str
    image:      bytes | None = None
    media_type: str = 'image/jpeg'
    is_error:   bool = False
    final:      bool = False        # decide accepted the decision: the call is over


@dataclass
class AgentCall:
    """One call of an agent skill from a tree"""

    contract: Contract
    inputs:   dict
    context:  Context
    phase:    str
    path:     str = ''


@dataclass
class CallImage:
    name:       str
    media_type: str
    data:       bytes
    path:       str | None = None   # where it is saved already; None: the link saves it for the recorder


class AgentSkill:
    """One decision of a tree, handed to a model or the operator. An agent skill's folder holds spec.py (its phases,
    decisions, fields and limits) and, for its own evidence, rules, checks and outputs, agent.py with one subclass.

    A call gathers the evidence (evidence()), lets the executor's own rules decide (rule()), else sends the call over the
    bridge and checks each decision (check()) until one is accepted; outputs() is what the tree gets.
    """

    phase_speech:    dict[str, str] = {}        # said when a call of the phase opens
    decision_speech: dict[str, str] = {}        # said when a decision is accepted

    def __init__(self, contract: Contract, spec: ModuleType, ctx: 'SkillContext | None' = None):
        self.contract = contract
        self.spec     = spec
        self.ctx      = ctx
        self.name     = contract.interface.skill or contract.name

    # ----- before the call -----

    def refuse(self, call: AgentCall) -> tuple[str, str] | None:
        """(why, what the robot says, '' for nothing) when this call fails at once without asking anyone; None to go on"""
        return None

    def evidence(self, call: AgentCall, evidence: dict):
        """Add what this skill knows to evidence, which already holds last_failure and history; runs in the agent's thread"""

    def rule(self, call: AgentCall, evidence: dict) -> tuple[dict, str] | None:
        """A decision the executor makes itself when the evidence makes it plain, with what the robot says: (decision
        with decision, reason and rule_name, speech). None asks the bridge"""
        return None

    def task_lines(self, call: AgentCall) -> list[str]:
        """The call as text, between its phase and its allowed decisions"""
        return []

    # ----- what goes over the link -----

    def request_fields(self, call: AgentCall, evidence: dict) -> dict:
        """Fields of the call beside the generic ones, for the bridge's prompt"""
        return {}

    def images(self, request: dict, session: 'AgentSession') -> list[CallImage]:
        return []

    # ----- the decision -----

    def check(self, decision: dict, session: 'AgentSession') -> str:
        """Why the executor refuses this decision (for the model to decide again); empty to accept it. It may fill
        defaults in decision"""
        return ''

    def outputs(self, decision: dict, call: AgentCall, evidence: dict) -> dict:
        """What the tree gets from a decision"""
        return {'decision': decision['decision'], 'reason': decision['reason']}

    def after_decision(self, decision: dict, call: AgentCall, evidence: dict):
        """Once a decision from the bridge is accepted, in the agent's thread"""

    # ----- the tree's record -----

    def record_fields(self, outputs: dict, call: AgentCall) -> dict:
        """Fields of the decision's record in the tree's history, beside the generic ones"""
        return {}

    def finished(self, call: AgentCall, status: Status, outputs: dict, record: dict):
        """After the decision is recorded, in the tree's thread; record is the call as an action record would be"""
