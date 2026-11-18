from typing import Protocol

from skill_registry import Contract

from ..tree.engine import Context, Status
from ..tree.leaf import SkillHandle, SkillLeaf
from ..skills.api import DoneHandle
from .api import AgentCall


class AgentRunner(Protocol):
    def start(self, contract: Contract, inputs: dict, context: Context, path: str = '') -> SkillHandle: ...


class AgentLeaf(SkillLeaf):
    """A leaf that runs an agent skill; it sees the tree's record, and its decision can end the whole tree.

    The handle's outputs carry decision and reason. The skill may refuse a call before it opens (AgentSkill.refuse);
    abort fails the tree.
    """

    refusal_speech = None                               # what the robot says for the skill's refusal; '' says nothing

    def _call(self) -> AgentCall:
        return AgentCall(self.contract, self.inputs, self.context, self.inputs.get('phase', ''), self.path)

    def _skill(self):
        return self.runner.skill(self.contract) if hasattr(self.runner, 'skill') else None

    def _start_call(self) -> SkillHandle:
        if self.runner is None:
            return DoneHandle(False, 'no agent is configured (agent_backend)')

        skill = self._skill()

        self.refusal_speech = None
        refusal             = skill.refuse(self._call()) if skill is not None else None

        if refusal is not None:
            message, self.refusal_speech = refusal
            return DoneHandle(False, message)

        return self.runner.start(self.contract, self.inputs, self.context, self.path)

    def _finish_call(self, status: Status):
        outputs  = self.handle.outputs
        decision = outputs.get('decision')

        if not decision:                                # the agent did not decide: nothing to keep, but say why
            self._say_failure()
            return

        call  = self._call()
        skill = self._skill()

        self.context.record({
            'time':      self.context.clock(),
            'skill':     self.contract.name,
            'path':      self.path,
            'phase':     self.inputs.get('phase', ''),
            'decision':  decision,
            'reason':    outputs.get('reason', ''),
            'rule':      bool(outputs.get('rule')),      # decided by the executor's rule, not the agent
            'rule_name': outputs.get('rule_name'),       # which rule: the rules count their own answers
            **(skill.record_fields(outputs, call) if skill is not None else {}),
        })

        if skill is not None:
            record = {'time': self.context.clock(), 'kind': 'condition', 'skill': self.contract.name, 'path': self.path,
                      'status': status.value, 'inputs': self.inputs, 'message': self.message, 'outputs': dict(outputs),
                      'elapsed_s': round(self.context.clock() - self.start_time, 2)}
            skill.finished(call, status, outputs, record)

        if decision == 'abort':
            self.context.abort(f'{self.contract.name}: {outputs.get("reason", "")}')

    def _say_failure(self):
        """A call that failed without a decision (no agent, refused, timeout, the bridge failed) goes on like an
        ask_help in the tree; the robot says why, so a person near it knows the helper is the problem"""
        text = self.message

        if self.refusal_speech is not None:
            said = self.refusal_speech
        elif text.startswith('no agent is configured'):
            said = 'I have no helper connected.'
        elif 'the agent bridge is unreachable' in text:     # link.UNREACHABLE
            said = 'My helper is still not connected. Going on without it.'
        elif text.startswith('timeout after'):
            said = 'My helper did not answer in time.'
        else:
            said = 'My helper could not decide.'

        if said and callable(getattr(self.runner, 'say', None)):
            self.runner.say(said)
