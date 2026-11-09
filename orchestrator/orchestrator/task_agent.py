"""The task agent's desk in the executor: a session outside it (Claude Code over ssh, with agent_cli) does a task
step by step (docs/task-mode.md).

A task sentence opens a task. The executor publishes it (latched JSON on OPEN_TOPIC: run_id, the sentence, blind,
the places with their kind, floor and note, what the knowledge store knows, the scratch folder) and waits for
commands on COMMAND_TOPIC:

- plan: the task plan, a list of subgoals; it goes in the trace (each version) and in plan.md in the scratch folder
- run: one skill as the next step, for a subgoal; the reply comes when the step ends, with its result
- write_skill: a contract (YAML text) for a new skill or a new version of the agent's own (skill_writer.py)
- skills: the skills that run can run now, as the executor has them
- done, give_up: end the task

Each command gets one reply on REPLY_TOPIC with its id. Limits: max_steps and max_task_s end the task as gave_up.
When no command comes for idle_s between steps (the session or its cloud link is gone), the robot says so once on
/speak and waits.

With the step gate on (the real robot, at first), a run waits for a person: the request goes out latched on
STEP_REQUEST_TOPIC, and agent_cli step approve|refuse answers on STEP_DECISION_TOPIC (reply on STEP_REPLY_TOPIC). agent_cli answers only at a
terminal (a tty), so the session that asked cannot answer itself by mistake. A refused step does not run.

This module needs no ROS: the executor wires the topics, calls command() from a worker thread (a run blocks until
its step ends) and check() from its timer.
"""

import time
import uuid
import threading

from typing import Callable
from pathlib import Path
from dataclasses import asdict

from .places import PLACE_INFO
from .tasks import Task, Tasks
from .planner import describe_catalog
from .skill_writer import SkillWriter
from .behavior_tree import Context


OPEN_TOPIC    = '/orchestrator/task/open'
COMMAND_TOPIC = '/orchestrator/task/command'
REPLY_TOPIC   = '/orchestrator/task/reply'
STEP_REQUEST_TOPIC  = '/orchestrator/step_request'
STEP_DECISION_TOPIC = '/orchestrator/step_decision'
STEP_REPLY_TOPIC    = '/orchestrator/step_decision/reply'
LOST_SPEECH   = 'I lost the connection to my planner. I will wait here. Please help me if you can.'


class StepGate:
    """A person approves or refuses each step before it runs"""

    def __init__(self, publish: Callable[[dict], None]):
        self.publish  = publish
        self.request  = None                    # the open request
        self.decision = None
        self.answered = threading.Event()
        self._lock    = threading.Lock()

    def ask(self, request: dict, still_open: Callable[[], bool]) -> dict:
        """Block until a person decides, or the task ends (cancel); returns {approve, state, reason}"""
        with self._lock:
            if self.request is not None:
                return {'approve': False, 'state': 'refused', 'reason': 'another step already waits for approval'}

            self.request  = {'id': uuid.uuid4().hex[:8], 'state': 'open', **request}
            self.decision = None
            self.answered.clear()

        self.publish(self.request)

        if not still_open():                    # it closed before the request was out: no cancel will come
            self.cancel('the task ended')

        self.answered.wait()

        with self._lock:
            request, decision, self.request = self.request, self.decision, None

        self.publish({**request, 'state': decision['state'], 'reason': decision['reason']})

        return decision

    def decide(self, message: dict) -> dict:
        """A decision for the open request; the reply says whether it was taken"""
        with self._lock:
            if self.request is None or message.get('id') != self.request['id'] or self.answered.is_set():
                return {'id': message.get('id'), 'accepted': False, 'message': 'no step with this id waits'}

            approve       = bool(message.get('approve'))
            self.decision = {'approve': approve, 'state': 'approved' if approve else 'refused',
                             'reason': str(message.get('reason', ''))}
            self.answered.set()

        return {'id': message['id'], 'accepted': True, 'message': self.decision['state']}

    def cancel(self, reason: str):
        """Refuse the open request, if any: its task ended"""
        with self._lock:
            request = self.request

        if request is not None:
            self.decide({'id': request['id'], 'approve': False, 'reason': reason})


class TaskDesk:
    def __init__(self, tasks: Tasks, publish_open: Callable[[dict], None], publish_reply: Callable[[dict], None],
                 speak: Callable[[str], None], known: Callable[[], str] = lambda: '', max_steps: int = 40,
                 max_task_s: float = 900.0, idle_s: float = 120.0, clock: Callable[[], float] = time.monotonic,
                 gate: StepGate | None = None, writer: SkillWriter | None = None):
        self.tasks         = tasks
        self.writer        = writer                  # None: write_skill is refused
        self.gate          = gate                    # None: steps run without a person's approval (sim)
        self.publish_open  = publish_open
        self.publish_reply = publish_reply
        self.speak         = speak
        self.known         = known                   # the knowledge store's prompt section, for the open task
        self.max_steps     = max_steps
        self.max_task_s    = max_task_s
        self.idle_s        = idle_s
        self.clock         = clock
        self.task          = None                    # the open task, and when it opened and last heard from the agent
        self.opened        = 0.0
        self.heard         = 0.0
        self.spoke_lost    = False
        self.plans         = 0
        self.busy          = 0                       # commands in progress: a session waiting for a step is not quiet
        self._lock         = threading.Lock()        # open and check on the spin thread, commands on worker threads

    def open(self, sentence: str, context: Context, blind: bool = False):
        if self.task is not None:
            self._close(self.task, 'replaced')          # a new task halts the old one (start_task)

        task = self.tasks.start_task('task', context, blind)

        with self._lock:
            self.task = task

        self.opened     = self.heard = self.clock()
        self.spoke_lost = False
        self.plans      = 0
        self.publish_open({
            'state':   'open',
            'run_id':  context.run_id,
            'task':    sentence,
            'blind':   blind,
            'places':  {key: {field: place[field] for field in PLACE_INFO if field in place} for key, place in sorted(context.blackboard.items())},
            'known':   self.known(),
            'scratch': context.scratch_dir,
            'limits':  {'max_steps': self.max_steps, 'max_task_s': self.max_task_s},
        })

    def command(self, message: dict):
        """One command from the session; the reply goes out with the command's id"""
        reply = {'id': message.get('id'), 'op': message.get('op')}

        with self._lock:
            self.busy += 1

        try:
            reply |= self._command(message)
        except (KeyError, TypeError, ValueError) as error:
            reply |= {'ok': False, 'message': f'bad command: {error!r}'}
        except OSError as error:
            reply |= {'ok': False, 'message': f'could not write: {error!r}'}
        finally:
            with self._lock:
                self.busy -= 1
                self.heard = self.clock()

        self.publish_reply(reply)

    def check(self):
        """From the executor's timer: the time limit, a session that went quiet, and a task that ended elsewhere"""
        task = self.task

        if task is None:
            return

        if self.tasks.task is not task:                  # cancelled, or a tree sent alone replaced it
            self._close(task, 'ended')
        elif self.clock() - self.opened > self.max_task_s:
            self._end(task, 'gave_up', f'the task took longer than max_task_s {self.max_task_s:.0f} s')
        elif not self.busy and not self.spoke_lost and self.clock() - self.heard > self.idle_s:
            self.spoke_lost = True
            self.speak(LOST_SPEECH)
            self.tasks.tracer.emit('agent_lost', f'no command for {self.idle_s:.0f} s; said so and waiting', level='warn')

    # ========== Commands ==========

    def _command(self, message: dict) -> dict:
        task = self.task

        if task is None or message.get('run_id') != task.run_id:
            return {'ok': False, 'message': 'no open task with this run_id; run agent_cli task wait'}

        self.heard      = self.clock()
        self.spoke_lost = False
        op              = message['op']

        if op == 'plan':
            return self._plan(task, [str(subgoal) for subgoal in message['subgoals']])

        if op == 'run':
            return self._run(task, message['skill'], dict(message.get('inputs') or {}), str(message.get('subgoal', '')))

        if op == 'write_skill':
            return self._write_skill(str(message['yaml']))

        if op == 'skills':
            return {'ok': True, 'catalog': describe_catalog(self.tasks.registry, self.tasks.essential_devices)}

        if op in ('done', 'give_up'):
            return self._end(task, 'done' if op == 'done' else 'gave_up', str(message.get('reason', '')))

        return {'ok': False, 'message': f'unknown op {op}; use plan, run, write_skill, skills, done or give_up'}

    def _write_skill(self, text: str) -> dict:
        if self.writer is None:
            return {'ok': False, 'message': 'write_skill is off here (no skill folder)'}

        reply = self.writer.write(text)
        self.tasks.tracer.emit('write_skill', reply['message'], level='info' if reply['ok'] else 'warn', ok=reply['ok'])

        return reply

    def _plan(self, task: Task, subgoals: list[str]) -> dict:
        if not subgoals:
            return {'ok': False, 'message': 'a plan needs at least one subgoal'}

        version = self.plans + 1
        text    = '\n'.join(f'{index}. {subgoal}' for index, subgoal in enumerate(subgoals, 1))

        if task.context.scratch_dir:
            Path(task.context.scratch_dir, 'plan.md').write_text(f'# Task plan, version {version}\n\n{text}\n')

        self.plans = version
        self.tasks.tracer.emit('plan', f'plan v{version}:\n{text}', subgoals=subgoals, version=version)

        return {'ok': True, 'message': f'plan v{self.plans} saved', 'version': self.plans}

    def _run(self, task: Task, skill: str, inputs: dict, subgoal: str) -> dict:
        if task.steps >= self.max_steps:
            return {'ok': False, 'message': f'max_steps {self.max_steps} reached: end the task with done or give_up'}

        if self.gate is not None:
            decision = self.gate.ask({'run_id': task.run_id, 'step': task.steps + 1, 'skill': skill, 'inputs': inputs,
                                      'subgoal': subgoal}, lambda: self.task is task)
            self.tasks.tracer.emit('step_gate', f'step {task.steps + 1} {skill}: {decision["state"]} {decision["reason"]}'.strip(),
                                   skill=skill, **decision)

            if not decision['approve']:
                return {'ok': False, 'status': 'refused', 'message': f'the operator refused the step: {decision["reason"]}'}

        result = self.tasks.run_skill(skill, inputs, subgoal=subgoal)

        return {'ok': result.status != 'error', **asdict(result)}

    def _end(self, task: Task, end: str, reason: str) -> dict:
        self._close(task, end)                           # first: else check() sees it end and closes it as 'ended'
        self.tasks.end_task(task, end, reason)

        return {'ok': True, 'message': f'the task ended: {end}'}

    def _close(self, task: Task, end: str):
        with self._lock:                                 # check() and a command can both close it; a new task may be open
            if self.task is not task:
                return

            self.task = None

        if self.gate is not None:
            self.gate.cancel('the task ended')

        self.publish_open({'state': 'closed', 'run_id': task.run_id, 'end': end})
