"""The task desk in the executor: the task agent, a session outside it (Claude Code over ssh, with agent_cli) does a task
step by step (docs/task-mode.md).

A task sentence opens a task. The executor publishes it (latched JSON on OPEN_TOPIC: run_id, the sentence, blind,
the places with their kind, floor and note, what the knowledge store knows, the scratch folder) and waits for
commands on COMMAND_TOPIC:

- plan: the task plan, a list of subgoals; it goes in the trace (each version) and in plan.md in the scratch folder
- run: one skill as the next step, for a subgoal; the reply comes when the step ends, with its result
- write_skill: a contract (YAML text) for a new skill or a new version of the agent's own, and for a code skill
  the Python that calls other skills (skill_writer.py)
- skills: the skills that run can run now, as the executor has them
- grade: stage the agent's grade of a claim that a step of this task followed (useful, misleading, wrong,
  not-applicable); the last grade of a claim wins. Also after the task ends, until the next task opens
- done, give_up: end the task; the reply names the applied claims with no grade
- commit: after the task ends, its staged grades go into the knowledge store together

Each task has its limits, set when it opens (self.limits). A discovery task (discovery: true in the request) has
DISCOVERY_LIMITS: no run, so it never moves the robot, and no time limit or idle speech, because reading code takes
long with no command between. The procedure is the discover agent skill (orchestrator/agent/skills/discover).

A run that follows claims of the knowledge store says so (applies, one or more ids). The step's end changes no
claim: a success can mean no more than "the message was published", and a failure can have another cause. The
agent grades each claim instead, and commits the grades after the task, like a database commit; useful and wrong
are weak evidence (knowledge.OUTCOME). Each grade keeps the code under the claim at its step, so a grade of a skill
that the agent rewrote later counts for no version. A task that opens commits the grades the last one left. The
agent writes claims itself, with the knowledge CLI (a session).

Each command gets one reply on REPLY_TOPIC with its id. Limits: max_steps and max_task_s end the task as gave_up.
When no command comes for idle_s between steps (the session or its cloud link is gone), the robot says so once on
/speak and waits.

With the step gate on (step_gate.py), a run waits for a person's approval. A refused step does not run.

This module needs no ROS: the executor wires the topics, calls command() from a worker thread (a run blocks until
its step ends) and check() from its timer.
"""

import time
import threading
import contextlib

from typing import Callable
from collections import deque
from pathlib import Path
from dataclasses import asdict

from skill_registry import contract_grammar

from .places import PLACE_INFO
from .knowledge import KnowledgeError, KnowledgeStore
from .tasks import Task, Tasks
from .tasks import describe_catalog
from skill_registry.skill_writer import SkillWriter
from .step_gate import StepGate
from .behavior_tree import Context


DISCOVERY_LIMITS = {'max_task_s': None, 'idle_s': None, 'run': False}   # a discovery task: see the module text

LOST_SPEECH   = 'I lost the connection to my planner. I will wait here. Please help me if you can.'

TRACE_LEVELS     = ('warn', 'error')   # the events a reply carries: what went wrong, not what went well
MAX_TRACE_EVENTS = 12                  # the last ones; a step that fails many times says the same thing


def trace_listener(lines: deque, run_id: str) -> Callable[[dict], None]:
    """Keeps what went wrong while a command runs, as (kind, text) and as the last MAX_TRACE_EVENTS: a record event
    holds its whole feedback list, so no event may stay alive for the length of a step. The tap is the node's, so it
    takes this run's events only: a replaced task's loop still emits, and its errors are not this task's"""
    def listen(event: dict):
        if event['level'] in TRACE_LEVELS and event.get('run_id', run_id) == run_id:
            lines.append((event['kind'], event['text']))

    return listen


def writing_text(folder: Path) -> str:
    """How the task session adds evidence to the knowledge store in folder"""
    return ('- On a run that follows claims, give their ids in applies. After the step, grade each one: '
            '`agent_cli grade <id> useful|misleading|wrong|not-applicable --reason R` (the last grade wins). Useful '
            'and wrong are evidence; misleading (it led you wrong here) and not-applicable (it did not fit this '
            'case) say that its text or tags are too wide. After the task ends, `agent_cli commit` puts the grades '
            'in the store.'
            f'\n- Write what you learned, for later runs: `knowledge --dir {folder} note "TEXT" --by agent:task '
            f'--run <run_id> --evidence step:<n> --about skill=<name>`. After a failure, write what to do '
            f'instead: "When <what failed and how it showed>, <what to do instead>." Confirm or contradict a '
            f'claim you checked: `knowledge --dir {folder} confirm|contradict <id> --by agent:task --run '
            f'<run_id> --evidence ...`.')


def place_text(info: dict) -> str:
    """map_info's kind, floor and note of a place; empty for a places file"""
    text = ', '.join(part for part in (info.get('kind'), info.get('floor')) if part)

    return (f'({text})' if text else '') + (f': {info["note"]}' if info.get('note') else '')


def task_summary(task: dict) -> str:
    """The open message as text: what the session reads (agent_cli task wait). It carries the runbook (what the
    robot is) and the contract grammar (what write_skill takes)"""
    if task['state'] != 'open':
        return f'no open task; the last one ({task["run_id"]}) ended: {task.get("end")}'

    places = ''.join(f'\n  {{{key}}} {place_text(info)}' for key, info in (task.get('places') or {}).items())
    lines  = [task['runbook']] if task.get('runbook') else []        # what the robot is, before what it must do
    lines += [
        f'task {task["run_id"]}' + (' (blind)' if task.get('blind') else '')
        + (' (discovery)' if task.get('discovery') else '') + f': {task["task"]}',
        'places on the blackboard:' + (places or ' none'),
        f'scratch folder: {task.get("scratch")}',
        f'limits: {task.get("limits")}',
    ]

    return '\n'.join(lines + [text for text in (task.get('known'), task.get('grammar')) if text])


class TaskDesk:
    def __init__(self, tasks: Tasks, publish_open: Callable[[dict], None], publish_reply: Callable[[dict], None],
                 speak: Callable[[str], None], knowledge: KnowledgeStore | None = None,
                 runbook: Callable[[], str] = lambda: '', max_steps: int = 40,
                 max_task_s: float = 900.0, idle_s: float = 120.0, clock: Callable[[], float] = time.monotonic,
                 gate: StepGate | None = None, writer: SkillWriter | None = None,
                 stale: Callable[[], dict[str, str]] = dict, ran: Callable[[str, str], None] = lambda name, run: None):
        self.tasks         = tasks
        self.writer        = writer                  # None: write_skill is refused
        self.gate          = gate                    # None: steps run without a person's approval (sim)
        self.publish_open  = publish_open
        self.publish_reply = publish_reply
        self.speak         = speak
        self.knowledge     = knowledge               # what is known, for the open task; the grades go in
        self.stale         = stale                   # every stale skill and why, in one read (skill_stamps.reasons)
        self.ran           = ran                     # a step that succeeded: kept with the skill's stamp as evidence
        self.runbook       = runbook                 # what the robot is, the operator's file; read again at each open
        self.task_limits   = {'max_steps': max_steps, 'max_task_s': max_task_s, 'idle_s': idle_s, 'run': True}
        self.limits        = self.task_limits        # the open task's: set at open
        self.clock         = clock
        self.task          = None                    # the open task, and when it opened and last heard from the agent
        self.ended         = None                    # the run_id of the task that ended last: graded until the next opens
        self.opened        = 0.0
        self.heard         = 0.0
        self.spoke_lost    = False
        self.plans         = 0
        self.busy          = 0                       # commands in progress: a session waiting for a step is not quiet
        self._lock         = threading.Lock()        # open and check on the spin thread, commands on worker threads
        self._grading      = threading.Lock()        # the staged file of a run: each command runs on its own thread

    def open(self, sentence: str, context: Context, blind: bool = False, discovery: bool = False) -> None:
        """Open a task and publish the open message; blind and discovery come from the task request"""
        if self.task is not None:
            self._close(self.task, 'replaced')          # a new task halts the old one (start_task)

        self._commit_left()
        task = self.tasks.start_task('discovery' if discovery else 'task', context, blind, bag=not discovery)

        with self._lock:
            self.task, self.ended = task, None

        self.opened     = self.heard = self.clock()
        self.spoke_lost = False
        self.plans      = 0
        self.limits     = self.task_limits | (DISCOVERY_LIMITS if discovery else {})
        opened          = {
            'state':   'open',
            'run_id':  context.run_id,
            'task':    sentence,
            'blind':   blind,
            'discovery': discovery,
            'places':  {key: {field: place[field] for field in PLACE_INFO if field in place} for key, place in sorted(context.blackboard.items())},
            'known':   self.known(),
            'runbook': self.runbook(),
            'grammar': contract_grammar() if self.writer else '',   # how to write a skill, if it may write one
            'scratch': context.scratch_dir,
            'limits':  self.limits,
        }
        self.publish_open(opened)
        self.tasks.tracer.emit('task_limits', f'limits: {self.limits}', limits=self.limits, discovery=discovery)

    def known(self) -> str:
        """The knowledge store's guide, and how the task writes to it"""
        return f'{self.knowledge.guide()}\n{writing_text(self.knowledge.folder)}' if self.knowledge else ''

    @contextlib.contextmanager
    def _thinking(self):
        """The session works on a command: not quiet. Each command that comes back counts as heard, also a
        refused one: the session is there"""
        with self._lock:
            self.busy += 1

        try:
            yield

            with self._lock:
                self.heard = self.clock()
        finally:
            with self._lock:
                self.busy -= 1

    def command(self, message: dict) -> dict:
        """One command from the session; the reply goes out with the command's id, and is returned"""
        reply = {'id': message.get('id'), 'op': message.get('op')}
        lines = deque(maxlen=MAX_TRACE_EVENTS)
        run   = str(message.get('run_id', ''))

        with self._thinking(), self.tasks.tracer.tap(trace_listener(lines, run)):
            try:
                reply |= self._command(message)
            except (KeyError, TypeError, ValueError) as error:
                reply |= {'ok': False, 'message': f'bad command: {error!r}'}
            except OSError as error:
                reply |= {'ok': False, 'message': f'could not write: {error!r}'}

        said  = str(reply.get('message', ''))
        trace = [f'{kind}: {text}' for kind, text in lines if text not in said]   # the message says that one already

        if trace:
            reply['trace'] = trace

        self.publish_reply(reply)

        return reply

    def check(self):
        """From the executor's timer: the time limit, a session that went quiet, and a task that ended elsewhere"""
        task = self.task

        if task is None:
            return

        if self.tasks.task is not task:                  # cancelled, or a tree sent alone replaced it
            self._close(task, 'ended')
        elif (limit := self.limits['max_task_s']) is not None and self.clock() - self.opened > limit:
            self._end(task, 'gave_up', f'the task took longer than max_task_s {limit:.0f} s')
        elif (idle := self.limits['idle_s']) is not None and not self.busy and not self.spoke_lost and self.clock() - self.heard > idle:
            self.spoke_lost = True
            self.speak(LOST_SPEECH)
            self.tasks.tracer.emit('agent_lost', f'no command for {idle:.0f} s; said so and waiting', level='warn')

    # ========== Commands ==========

    def _command(self, message: dict) -> dict:
        task = self.task
        op   = message['op']

        if task is not None and message.get('run_id') == task.run_id:
            self.heard, self.spoke_lost = self.clock(), False

        if op in ('grade', 'commit'):                    # also after the task ends, until the next task opens
            return self._grading_op(op, message)

        if task is None or message.get('run_id') != task.run_id:
            return {'ok': False, 'message': 'no open task with this run_id; run agent_cli task wait'}

        if op == 'plan':
            return self._plan(task, [str(subgoal) for subgoal in message['subgoals']])

        if op == 'run':
            if not self.limits['run']:
                return {'ok': False, 'message': 'a discovery task runs no skill: it never moves the robot. Write skills; '
                                                'a task session tries them'}

            applies = message.get('applies') or []

            if not isinstance(applies, list):
                return {'ok': False, 'message': 'applies is a list of claim ids; the step did not run'}

            return self._run(task, message['skill'], dict(message.get('inputs') or {}), str(message.get('subgoal', '')),
                             list(dict.fromkeys(map(str, applies))))

        if op == 'write_skill':
            return self._write_skill(str(message['yaml']), str(message.get('code', '')))

        if op == 'skills':
            return {'ok': True, 'catalog': describe_catalog(self.tasks.registry, self.tasks.essential_devices, self.stale())}

        if op in ('done', 'give_up'):
            return self._end(task, 'done' if op == 'done' else 'gave_up', str(message.get('reason', '')))

        return {'ok': False, 'message': f'unknown op {op}; use plan, run, grade, write_skill, skills, done, give_up or commit'}

    def _write_skill(self, text: str, code: str = '') -> dict:
        if self.writer is None:
            return {'ok': False, 'message': 'write_skill is off here (no skill folder)'}

        reply = self.writer.write(text, code)
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

    def _run(self, task: Task, skill: str, inputs: dict, subgoal: str, applies: list[str] = ()) -> dict:
        if task.steps >= self.limits['max_steps']:
            return {'ok': False, 'message': f'max_steps {self.limits["max_steps"]} reached: end the task with done or give_up'}

        if applies and (problem := self._not_live(applies)):
            return {'ok': False, 'message': f'the step did not run: {problem}'}

        if self.gate is not None:
            decision = self.ask_step({'skill': skill, 'inputs': inputs, 'subgoal': subgoal},
                                     lambda: self.task is task)

            if not decision['approve']:
                return {'ok': False, 'status': 'refused', 'message': f'the operator refused the step: {decision["reason"]}'}

        self._note_stale(skill)
        result = self.tasks.run_skill(skill, inputs, subgoal=subgoal)

        if result.status == 'success':
            self._keep(skill, task.run_id)

        reply = {'ok': result.status != 'error', **asdict(result)}

        if applies and result.status in ('success', 'failure'):    # an error never ran: there is nothing to grade
            if self.task is not task:                              # it ended while the step ran: its grades are committed
                reply['applies'] = 'not kept: the task ended while the step ran'
            else:
                try:
                    with self._grading:                            # with the code under each claim now: the version graded
                        self.knowledge.stage_applied(task.run_id, applies, skill, task.steps)

                    reply['applies'] = f'grade each (agent_cli grade): {", ".join(applies)}'
                except Exception as error:                         # the step HAS run: never fail it for bookkeeping
                    reply['applies'] = f'not kept, so these cannot be graded: {error!r}'

        return reply

    def _not_live(self, applies: list[str]) -> str:
        """Why a run cannot follow these claims, or ''"""
        if self.knowledge is None:
            return 'applies needs a knowledge store, and there is none here'

        try:
            self.knowledge.check_live(applies)
        except KnowledgeError as error:
            return str(error)

        return ''

    def _grading_op(self, op: str, message: dict) -> dict:
        """grade and commit: for a run with staged entries, during the task or after it ends"""
        run_id = str(message.get('run_id'))

        if self.knowledge is None:
            return {'ok': False, 'message': 'there is no knowledge store here'}

        if run_id not in (self.task and self.task.run_id, self.ended):
            return {'ok': False, 'message': f'{run_id} is not the open task or the task that ended last'}

        if op == 'commit' and self.task is not None and self.task.run_id == run_id:
            return {'ok': False, 'message': 'the task is still open: commit after done or give_up'}

        with self._grading:
            if op == 'commit':
                return self._commit(run_id, '')

            try:
                self.knowledge.stage(run_id, str(message['claim']), str(message['grade']), str(message.get('reason', '')).strip())
            except KnowledgeError as error:
                return {'ok': False, 'message': str(error)}

        said = f'{message["claim"]} staged: {message["grade"]}; agent_cli commit after the task ends'
        self.tasks.tracer.emit('knowledge', said, claim=message['claim'], grade=message['grade'], run_id=run_id)

        return {'ok': True, 'message': said}

    def _commit(self, run_id: str, when: str) -> dict:
        """The staged grades of a run go into the store; the caller holds _grading"""
        said, ungraded = self.knowledge.commit(run_id)
        self.tasks.tracer.emit('knowledge', f'committed {run_id}{when}: ' + ('; '.join(said) or 'no grades'),
                               run_id=run_id, claims=said)

        return {'ok': True, 'message': '\n'.join(said) or 'no grades to commit',
                **({'not_graded': ungraded} if ungraded else {})}

    def _commit_left(self) -> None:
        """At a task open: the grades that an earlier task staged and did not commit go in first"""
        if self.knowledge is None:
            return

        with self._grading:
            for run_id in self._staged_runs():
                try:
                    self._commit(run_id, ' at the next open')
                except Exception as error:                # bookkeeping must never stop a task from opening
                    self.tasks.tracer.emit('knowledge', f'could not commit {run_id}, set aside: {error!r}', level='warn',
                                           run_id=run_id)
                    self._set_aside(run_id)

    def _staged_runs(self) -> list[str]:
        try:
            return self.knowledge.staged_runs()
        except OSError:
            return []

    def _set_aside(self, run_id: str) -> None:
        try:
            self.knowledge.set_aside(run_id)
        except (OSError, KnowledgeError) as error:
            self.tasks.tracer.emit('knowledge', f'could not set {run_id} aside: {error!r}', level='warn', run_id=run_id)

    def _note_stale(self, skill: str) -> None:
        """Say in the trace and the bundle that a step used a stale skill, not only in the catalog that the session reads"""
        try:
            if reason := self.stale().get(skill):
                self.tasks.tracer.emit('stale_skill', f'{skill}: {reason}', level='warn', skill=skill)
        except OSError as error:                     # bookkeeping must never decide whether a step runs
            self.tasks.tracer.emit('stale_skill', f'{skill}: could not be checked: {error!r}', level='warn')

    def _keep(self, skill: str, run_id: str) -> None:
        """Keep that the skill ran. The step HAS happened: a failure to write it down must not be reported as a
        failed command, or the agent runs the action again (command() turns an OSError into ok: false)"""
        try:
            self.ran(skill, run_id)
        except OSError as error:
            self.tasks.tracer.emit('stamp_error', f'{skill}: the run was not kept: {error!r}', level='warn')

    def ask_step(self, request: dict, still_open=None, step=None) -> dict:
        """Ask a person about one step and trace the answer; also used for a call inside a code skill"""
        if self.gate is None:
            return {'approve': True, 'state': 'approved', 'reason': ''}

        task     = self.task
        full     = {'run_id': task.run_id if task else None, 'step': step or (task.steps + 1 if task else 0)} | request
        decision = self.gate.ask(full, still_open or (lambda: self.task is task))
        self.tasks.tracer.emit('step_gate', f'{request.get("skill")}: {decision["state"]} {decision["reason"]}'.strip(),
                               skill=request.get('skill'), subgoal=request.get('subgoal', ''), step=full['step'],
                               run_id=full['run_id'], **decision)   # its own run: a late refusal of a closed task

        return decision

    def _end(self, task: Task, end: str, reason: str) -> dict:
        self._close(task, end)                           # first: else check() sees it end and closes it as 'ended'
        self.tasks.end_task(task, end, reason)
        message  = f'the task ended: {end}'

        staged   = self._staged(task.run_id)

        if staged:                                       # said, not enforced: a grade is the agent's judgement
            ungraded = [claim_id for claim_id, entry in staged.items() if not entry['grade']]
            message += '; grade what is left, then agent_cli commit' + (f'; not graded: {", ".join(ungraded)}' if ungraded else '')

        return {'ok': True, 'message': message}

    def _staged(self, run_id: str) -> dict:
        try:
            return self.knowledge.staged(run_id) if self.knowledge else {}
        except (OSError, ValueError, KnowledgeError):
            return {}

    def _close(self, task: Task, end: str):
        with self._lock:                                 # check() and a command can both close it; a new task may be open
            if self.task is not task:
                return

            self.task, self.ended = None, task.run_id

        if self.gate is not None:
            self.gate.cancel('the task ended')

        self.publish_open({'state': 'closed', 'run_id': task.run_id, 'end': end})
