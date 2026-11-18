"""Agent skills: one decision of a tree, handed to a model or to the operator through the agent bridge.

The tree calls an agent skill like any other leaf (agent/leaf.py). The executor gathers the evidence once (the failure
the tree recorded, the history, and what the skill adds), the backend sends it with the task, and one decision comes
back. The executor checks it, and the tree acts on it. What a skill adds, checks and outputs is in its folder's
agent.py; this module knows no skill.
"""

import json
import threading

from typing import Callable, Protocol
from concurrent.futures import ThreadPoolExecutor

from skill_registry import Contract

from ..tree.engine import Context, Status
from ..skills.loader import SkillLibrary
from .api import AgentCall, AgentSkill, ToolOutput
from .protocol import load_spec


class AgentError(RuntimeError):
    pass


class AgentBackend(Protocol):
    name: str

    def run(self, task: str, session: 'AgentSession', should_stop: Callable[[], bool], on_event: Callable[..., None]): ...


class AgentSession:
    """One agent call: its skill, the evidence gathered for it, and the decision once the executor accepts it.
    The backend reads the evidence as it is and sends each decision it gets through decide()"""

    def __init__(self, skill: AgentSkill, call: AgentCall, evidence: dict, on_event):
        self.skill    = skill
        self.call     = call
        self.phase    = call.phase
        self.inputs   = call.inputs
        self.evidence = evidence
        self.on_event = on_event
        self.decision = None

    def camera(self) -> tuple[bytes, str] | None:
        """The latest camera frame and its media type, or None when there is none"""
        camera = getattr(self.skill.ctx, 'camera', None)

        if camera is None:
            return None

        if camera.jpeg is None:
            self.on_event('agent_camera', 'no camera image: no camera frame yet', level='warn')
            return None

        return camera.jpeg, 'image/jpeg'

    def decide(self, arguments: dict) -> ToolOutput:
        """Check a decision; final when accepted. A refusal says why, for the backend to ask again"""
        output = self._decide(dict(arguments))

        if output.is_error:
            self.on_event('agent_refused', output.text[:300], level='warn')

        return output

    def _decide(self, arguments: dict) -> ToolOutput:
        fields  = getattr(self.skill.spec, 'FIELDS', {})
        unknown = set(arguments) - {'decision', 'reason', *fields}

        if unknown or 'decision' not in arguments or 'reason' not in arguments:
            return ToolOutput(f'bad decision fields: {sorted(unknown) or "decision and reason are required"}', is_error=True)

        if self.decision is not None:
            return ToolOutput('you have already decided', is_error=True)

        allowed = self.skill.spec.PHASES[self.phase]

        if arguments['decision'] not in allowed:
            return ToolOutput(f'decision must be one of {list(allowed)} in phase {self.phase}', is_error=True)

        if not str(arguments['reason']).strip():
            return ToolOutput('give a reason', is_error=True)

        decision = {'decision': arguments['decision'], 'reason': str(arguments['reason']).strip(),
                    **{name: arguments.get(name) for name in fields}}

        if refusal := self.skill.check(decision, self):
            return ToolOutput(refusal, is_error=True)

        self.decision = decision
        self.on_event('agent_decision', f'{decision["decision"]}: {decision["reason"]}', decision=self.decision)

        return ToolOutput(f'decision recorded: {decision["decision"]}', final=True)


class AgentHandle:
    """One running agent call, polled by the leaf. Cancelling tells the backend to drop it"""

    def __init__(self, pool: ThreadPoolExecutor, work: Callable[[threading.Event], dict], going_on: set):
        self.status   = Status.RUNNING
        self.message  = ''
        self.outputs  = {}
        self.going_on = going_on
        self._stop    = threading.Event()
        self._future  = pool.submit(work, self._stop)

    def poll(self) -> Status:
        if self.status != Status.RUNNING or not self._future.done():
            return self.status

        try:
            self.outputs = self._future.result()
        except Exception as error:                      # the backend, the network, or no decision
            self.status, self.message = Status.FAILURE, f'agent failed: {error}'
            return self.status

        decision     = self.outputs['decision']
        self.status  = Status.SUCCESS if decision in self.going_on else Status.FAILURE
        self.message = f'{decision}: {self.outputs["reason"]}'

        return self.status

    def cancel(self):
        self._stop.set()


class Agent:
    """Runs agent skills for the executor: one call at a time, each in a thread"""

    def __init__(self, backend: AgentBackend, ctx=None, library: SkillLibrary | None = None, on_event=None,
                 speak: Callable[[str], None] | None = None):
        self.backend  = backend
        self.ctx      = ctx
        self.library  = library or SkillLibrary()
        self.speak    = speak
        self.on_event = on_event or (lambda kind, text, **data: None)
        self.skills: dict[str, AgentSkill] = {}
        self._pool    = ThreadPoolExecutor(max_workers=1)

    def skill(self, contract: Contract) -> AgentSkill:
        """The contract's agent skill, made once: its folder's AgentSkill subclass, else the plain one"""
        if contract.name not in self.skills:
            name       = contract.interface.skill or contract.name
            skill_type = self.library.find(name, 'agent', AgentSkill) or AgentSkill
            self.skills[contract.name] = skill_type(contract, load_spec(name, self.library.packages), self.ctx)

        return self.skills[contract.name]

    def start(self, contract: Contract, inputs: dict, context: Context, path: str = '') -> AgentHandle:
        skill    = self.skill(contract)
        call     = AgentCall(contract, inputs, context, inputs.get('phase') or next(iter(skill.spec.PHASES)), path)
        evidence = json.loads(json.dumps({'last_failure': context.last_failure, 'history': context.history}, default=str))

        def work(stop: threading.Event) -> dict:
            # (1) The evidence, gathered once
            skill.evidence(call, evidence)

            # (2) A rule decides without the bridge when the evidence makes the answer plain
            if (ruled := skill.rule(call, evidence)) is not None:
                decision, speech = ruled
                self.on_event('agent_rule', decision['reason'], phase=call.phase)
                self._say(speech)
                return {**skill.outputs(decision, call, evidence), 'rule': True, 'rule_name': decision.get('rule_name')}

            # (3) The call: the backend sends the evidence and brings back one decision that decide accepted
            session = AgentSession(skill, call, evidence, self.on_event)

            self.on_event('agent_start', f'{contract.name} ({call.phase}) on {self.backend.name}', phase=call.phase)
            self._say(skill.phase_speech.get(call.phase))
            self.backend.run(task_text(skill, call), session, stop.is_set, self.on_event)

            if session.decision is None:
                raise AgentError('the agent ended without a decision')

            self._say(skill.decision_speech.get(session.decision['decision']))
            skill.after_decision(session.decision, call, evidence)

            return skill.outputs(session.decision, call, evidence)

        return AgentHandle(self._pool, work, set(skill.spec.GOING_ON))

    def say(self, text: str):
        """Say one sentence for the agent leaf (e.g. why a call failed)"""
        self._say(text)

    def _say(self, text: str | None):
        if text and self.speak:
            self.speak(text)
            self.on_event('agent_say', text)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def task_text(skill: AgentSkill, call: AgentCall) -> str:
    """The call as text: its phase, what the skill says about it, and the allowed decisions"""
    lines = [f'Phase: {call.phase}.', *skill.task_lines(call), f'Allowed decisions: {", ".join(skill.spec.PHASES[call.phase])}.']

    return '\n'.join(lines)
