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
- grade_step: stage the agent's grade of a step's skill (worked, failed, misused, blocked; step_grades.py), the same
  way: it is the evidence a skill graduates on
- done, give_up: end the task; the reply names the applied claims with no grade. With the step gate on, a person
  approves the end first; a refused end keeps the task open, and the reply gives the person's reason
- commit: after the task ends, its staged grades go into the knowledge store and the step grade log together

Each task has its limits, set when it opens (self.limits). A discovery task (discovery: true in the request) has
DISCOVERY_LIMITS: no run, so it never moves the robot, and no time limit or idle speech, because reading code takes
long with no command between. The procedure is the discover agent skill (orchestrator/agent/skills/discover).

A run that follows claims of the knowledge store says so (applies, one or more ids). The step's end changes no
claim: a success can mean no more than "the message was published", and a failure can have another cause. The
agent grades each claim instead, and commits the grades after the task, like a database commit; useful and wrong
are weak evidence (knowledge.OUTCOME). Each grade keeps the code under the claim at its step, so a grade of a skill
that the agent rewrote later counts for no version. A task that opens commits the grades the last one left. The
agent writes claims itself, with the knowledge CLI (a session).

Each command gets one reply on REPLY_TOPIC with its id. A note that the operator sent for the open task (note()) goes
in the next reply, as operator_notes; the trace keeps when it came and when the agent got it. Limits: max_steps and max_task_s (when set) end the task as gave_up.
When no command comes for idle_s between steps (the session or its cloud link is gone), the robot says so once on
/speak and waits.

With the step gate on (step_gate.py), a run, done and give_up wait for a person's approval. A refused step does not
run.

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
from .topics import MAX_NOTE
from .knowledge import KnowledgeError, KnowledgeStore, StagedRuns
from .step_grades import StepGrades
from .graduation import Graduation, at_run_time
from .tasks import Task, Tasks
from .tasks import describe_catalog
from skill_registry.skill_writer import SkillWriter
from .step_gate import StepGate
from .behavior_tree import Context


DISCOVERY_LIMITS = {'max_task_s': None, 'idle_s': None, 'run': False}   # a discovery task: see the module text

LOST_SPEECH   = 'I lost the connection to my planner. I will wait here. Please help me if you can.'
TRUSTED       = {'approve': True, 'state': 'approved', 'reason': 'it is tested, so the gate did not ask', 'by': 'maturity'}
UNREAD        = 'the stale skills could not be read'

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


STEP_GRADING = ('- After the task (or after a step), grade the skill of each step that ran: `agent_cli grade-step <n> '
                'worked|failed|misused|blocked --reason R` (the last grade wins). worked: it did what the subgoal '
                'needed, and you saw it; failed: the skill did not do its job; misused: you chose the wrong skill or '
                'gave wrong inputs; blocked: a device or the place was the cause. Only worked and failed count for '
                'the skill, for its version at that step. `agent_cli commit` keeps them with the claim grades.')

RETROSPECT   = ('- Read a run again: `retrospect show <run_id>` gives each step with its calls, their inputs, the '
                'feedback of a failure, and its grade. `retrospect blame <run_id>` gives the evidence of a failure in time '
                'order: the calls with their feedback, and your words then; you find the cause and write it as a note. '
                '`retrospect list` gives the earlier runs. Read them before you write a tree or a "When ..., do ..." note.')


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


EXECUTOR       = 'the executor'      # who refused a step that no person saw
REPLY_FEEDBACK = 5         # feedback samples of a failed call in a step reply; the trace and the run keep them all


def _step_reply(result) -> dict:
    """The reply to a step. One that did not succeed has a reason; a failed call keeps only its last feedback, with a
    count: on 2026-11-20 a walk's reply held 180 s of feedback, and the agent cut it with grep and lost the cause"""
    reply   = {'ok': result.status != 'error', **asdict(result)}
    failure = reply['failure']

    for record in (failure, failure and failure.get('checked_call')):
        if isinstance(record, dict) and record.get('feedback'):
            record.update(feedback=record['feedback'][-REPLY_FEEDBACK:], feedback_count=len(record['feedback']))

    if result.status != 'success':
        reply['reason'] = _failure_reason(failure) if failure else result.message

    return reply


def _failure_reason(failure: dict) -> str:
    """The failed call's message; a call with none is named, with the call it checks, so a reason is never only
    'stepN_skill: failure'"""
    if failure.get('message'):
        return failure['message']

    checks = f' (the check of {failure["checks"]})' if failure.get('checks') else ''
    return f'{failure.get("skill")}{checks} ended {failure.get("status")} with no message: see failure'


def _refused(message: str, reason: str, by: str) -> dict:
    """A step that did not run. The reason has its own field: on 2026-11-20 an agent kept only "status" and "reason"
    of the reply (grep), lost the operator's reason inside the message, and sent the same step again"""
    return {'ok': False, 'status': 'refused', 'message': message, 'reason': reason, 'by': by}


class TaskDesk:
    def __init__(self, tasks: Tasks, publish_open: Callable[[dict], None], publish_reply: Callable[[dict], None],
                 speak: Callable[[str], None], knowledge: KnowledgeStore | None = None,
                 runbook: Callable[[], str] = lambda: '', max_steps: int = 40,
                 max_task_s: float | None = 1800.0, idle_s: float = 120.0, clock: Callable[[], float] = time.monotonic,
                 gate: StepGate | None = None, writer: SkillWriter | None = None,
                 stale: Callable[[], dict[str, str]] = dict, ran: Callable[[str, str], None] = lambda name, run: None,
                 steps: StepGrades | None = None, composition: Callable[[str], dict] = lambda name: {},
                 graduation: Graduation | None = None, trust_tested: bool = True):
        self.tasks         = tasks
        self.writer        = writer                  # None: write_skill is refused
        self.gate          = gate                    # None: steps run without a person's approval (sim)
        self.publish_open  = publish_open
        self.publish_reply = publish_reply
        self.speak         = speak
        self.knowledge     = knowledge               # what is known, for the open task; the grades go in
        self.stale         = stale                   # every stale skill and why, in one read (skill_stamps.reasons)
        self.ran           = ran                     # a step that succeeded: kept with the skill's stamp as evidence
        self.steps         = steps                   # the grades of each step's skill; None: steps are not graded
        self.graduation    = graduation              # what each skill earned (graduation.py); None: the catalog says no grades
        self.trust_tested  = trust_tested            # a tested skill that is not stale runs with no question at the gate
        self.composition   = composition             # what a code skill calls, and its versions (skill_review.composition)
        self.runbook       = runbook                 # what the robot is, the operator's file; read again at each open
        max_task_s         = max_task_s if max_task_s and max_task_s > 0 else None     # 0 or less: no time limit
        self.task_limits   = {'max_steps': max_steps, 'max_task_s': max_task_s, 'idle_s': idle_s, 'run': True}
        self.limits        = self.task_limits        # the open task's: set at open
        self.clock         = clock
        self.task          = None                    # the open task, and when it opened and last heard from the agent
        self.ended         = None                    # the run_id of the task that ended last: graded until the next opens
        self.opened        = 0.0
        self.heard         = 0.0
        self.spoke_lost    = False
        self.plans         = 0
        self.notes         = []                      # the operator's notes for the open task, not yet in a reply
        self.busy          = 0                       # commands in progress: a session waiting for a step is not quiet
        self._lock         = threading.Lock()        # open and check on the spin thread, commands on worker threads
        self._grading      = threading.Lock()        # the staged files of a run: each command runs on its own thread

    def open(self, sentence: str, context: Context, blind: bool = False, discovery: bool = False) -> None:
        """Open a task and publish the open message; blind and discovery come from the task request"""
        if self.task is not None:
            self._close(self.task, 'replaced')          # a new task halts the old one (start_task)

        self._commit_left()
        task = self.tasks.start_task('discovery' if discovery else 'task', context, blind, bag=not discovery)

        with self._lock:
            left, last            = self.notes, self.ended
            self.task, self.ended = task, None
            self.notes            = []

        if left:                                         # never in a reply: the trace says so, for the nudge count
            self.tasks.tracer.emit('operator_note', f'{len(left)} note(s) not given: a new task opened', level='warn',
                                   state='dropped', run_id=last, notes=left)

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
        parts = [self.knowledge.guide(), writing_text(self.knowledge.folder), RETROSPECT] if self.knowledge else []

        if self.steps and self.limits['run']:             # a discovery task runs no step: nothing to grade
            parts.append(STEP_GRADING)

        return '\n'.join(parts)

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

        if notes := self._take_notes(run):
            reply['operator_notes'] = notes

        self.publish_reply(reply)

        return reply

    def note(self, run_id: str, text: str) -> bool:
        """An operator's note for the open task: it goes in the next reply to the agent. The agent waits on a command
        most of the time, so it gets the note when that command ends. False when run_id is not the open task"""
        text = ' '.join(text.split())[:MAX_NOTE]

        with self._lock:
            task = self.task
            ok   = task is not None and task.run_id == run_id and bool(text)

            if ok:
                self.notes.append(text)

        if not ok:
            self.tasks.tracer.emit('operator_note', f'note not given: {"it is empty" if text else "no open task " + run_id}',
                                   level='warn', state='refused', run_id=run_id, note=text)
            return False

        self.tasks.tracer.emit('operator_note', f'note for the agent: {text}', state='queued', run_id=run_id, note=text,
                               step=task.steps)
        return True

    def _take_notes(self, run_id: str) -> list[str]:
        """The task's notes for a reply to it, once; also the reply that ends it (done), and a grade after it"""
        with self._lock:
            if not self.notes or run_id not in (self.task and self.task.run_id, self.ended):
                return []

            notes, self.notes = self.notes, []

        self.tasks.tracer.emit('operator_note', f'the agent got {len(notes)} note(s)', state='given', run_id=run_id,
                               notes=notes)
        return notes

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

        if op in ('grade', 'grade_step', 'commit'):      # also after the task ends, until the next task opens
            return self._grading_op(op, message)

        if task is None or message.get('run_id') != task.run_id:
            return {'ok': False, 'message': 'no open task with this run_id; run agent_cli task wait'}

        if op == 'plan':
            return self._plan(task, [str(subgoal) for subgoal in message['subgoals']])

        if op == 'run':
            if not self.limits['run']:
                return _refused('a discovery task runs no skill: it never moves the robot. Write skills; a task session '
                                'tries them', 'a discovery task runs no skill', EXECUTOR)

            applies = message.get('applies') or []

            if not isinstance(applies, list):
                return _refused('applies is a list of claim ids; the step did not run', 'applies is not a list', EXECUTOR)

            return self._run(task, message['skill'], dict(message.get('inputs') or {}), str(message.get('subgoal', '')),
                             list(dict.fromkeys(map(str, applies))))

        if op == 'write_skill':
            return self._write_skill(str(message['yaml']), str(message.get('code', '')), str(message.get('tree', '')))

        if op == 'skills':
            stale = self._stale_now('the catalog')
            trust = {contract.name: self._trust(contract.name, stale) for contract in self.tasks.registry}
            return {'ok': True, 'catalog': describe_catalog(
                self.tasks.registry, self.tasks.essential_devices,
                {name: reason for name, found in trust.items() if (reason := found['stale'] or (UNREAD if stale is None else ''))},
                self.graduation and self.graduation.of,
                refused={name: found['deprecated'] for name, found in trust.items() if found['deprecated']})}

        if op in ('done', 'give_up'):
            return self._ask_end(task, op, str(message.get('reason', '')))

        return {'ok': False, 'message': f'unknown op {op}; use plan, run, grade, grade_step, write_skill, skills, done, give_up or commit'}

    def _write_skill(self, text: str, code: str = '', tree: str = '') -> dict:
        if self.writer is None:
            return {'ok': False, 'message': 'write_skill is off here (no skill folder)'}

        reply = self.writer.write(text, code, tree)
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
            return _refused(f'max_steps {self.limits["max_steps"]} reached: end the task with done or give_up',
                            f'max_steps {self.limits["max_steps"]} reached', EXECUTOR)

        if applies and (problem := self._not_live(applies)):
            return _refused(f'the step did not run: {problem}', problem, EXECUTOR)

        seen  = self.composition(skill)                  # one read: the trust, the gate and the check after it
        trust = self._trust(skill, self._stale_now(skill), seen)

        if bad := trust['deprecated']:
            reason = f'{bad} is deprecated'
            self.tasks.tracer.emit('step_gate', f'{skill}: refused, {reason}', level='warn', skill=skill,
                                   subgoal=subgoal, step=task.steps + 1, run_id=task.run_id, state='refused',
                                   reason=reason, by='maturity')
            return _refused(f'{reason}: its grades on the robot show that it fails. The step did not run: '
                            f'write a new version (write_skill), or use another skill', reason, 'maturity')

        if self.gate is not None:
            if problem := self.tasks.check_step(task, skill, inputs):
                # a person sees only a step that can run: on 2026-11-20 the operator approved three that then did not validate
                self.ask_step({'skill': skill, 'inputs': inputs, 'subgoal': subgoal},
                              decided={'approve': False, 'state': 'refused', 'reason': problem, 'by': 'validator', 'level': 'warn'})
                return _refused(f'{problem}. The step did not run, and the operator was not asked', problem, 'validator')

            trusted  = self.trust_tested and trust['maturity'] == 'tested' and trust['earned']
            decision = self.ask_step({'skill': skill, 'inputs': inputs, 'subgoal': subgoal,
                                      **({'calls': seen['calls'], 'unknown_calls': seen['unknown']} if seen.get('versions') else {})},
                                     lambda: self.task is task, decided=TRUSTED if trusted else None)

            if not decision['approve']:
                return _refused(f'the operator refused the step: {decision["reason"]}', decision['reason'],
                                decision.get('by', EXECUTOR))

            if problem := self.tasks.check_step(task, skill, inputs):      # a skill written while the person decided
                self.tasks.tracer.emit('step_gate', f'{skill}: refused after the approval, {problem}', level='warn',
                                       skill=skill, state='refused', run_id=task.run_id)
                return _refused(f'{problem}, after the operator approved it: the step did not run', problem, 'validator')

            if self.composition(skill).get('versions') != seen.get('versions'):
                self.tasks.tracer.emit('step_gate', f'{skill}: refused, its code changed after the approval', level='warn',
                                       skill=skill, state='refused', run_id=task.run_id)
                return _refused(f'{skill} changed after the operator approved it: the step did not run; run it again',
                                f'{skill} changed after the approval', EXECUTOR)

            if not trusted and (again := self._stale_now(skill)) is not None:    # a person may have waited a while
                trust['stale'] = self._stale_reason(skill, seen.get('calls', []), again)

        if reason := trust['stale']:                     # in the trace and the bundle, not only in the catalog
            self.tasks.tracer.emit('stale_skill', f'{skill}: {reason}', level='warn', skill=skill)

        version = self._version(skill)                   # before the step: the version that runs, not a write during it
        result  = self.tasks.run_skill(skill, inputs, subgoal=subgoal)

        if result.status == 'success':
            self._keep(skill, task.run_id)

        reply = _step_reply(result)

        if result.status not in ('success', 'failure'):           # an error never ran: there is nothing to grade
            return reply

        stages = [('grade', lambda: self.steps.stage_step(task.run_id, task.steps, skill, version, result.status),
                   f'grade this step after it (agent_cli grade-step {task.steps})')] if self.steps else []

        if applies:                                               # with the code under each claim now: the version graded
            stages.append(('applies', lambda: self.knowledge.stage_applied(task.run_id, applies, skill, task.steps),
                           f'grade each (agent_cli grade): {", ".join(applies)}'))

        for field, stage, said in stages:
            try:
                with self._grading:                                # with the check: a commit runs under this lock too
                    if self.task is not task:                      # it ended while the step ran: it may be committed
                        reply[field] = 'not kept: the task ended while the step ran'
                        continue

                    stage()

                reply[field] = said
            except Exception as error:                             # the step HAS run: never fail it for bookkeeping
                reply[field] = f'not kept, so this cannot be graded: {error!r}'

        return reply

    def _version(self, skill: str) -> str | None:
        try:
            return self.steps.version(skill) if self.steps else None
        except OSError:                                            # bookkeeping must never decide whether a step runs
            return None

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
        """grade, grade_step and commit: for a run with staged entries, during the task or after it ends"""
        run_id = str(message.get('run_id'))
        store, what = {'grade': (self.knowledge, 'claim'), 'grade_step': (self.steps, 'step')}.get(op, (None, None))

        if what and store is None:
            return {'ok': False, 'message': f'no {what} is graded here: there is no store for it'}

        if run_id not in (self.task and self.task.run_id, self.ended):
            return {'ok': False, 'message': f'{run_id} is not the open task or the task that ended last'}

        if op == 'commit' and self.task is not None and self.task.run_id == run_id:
            return {'ok': False, 'message': 'the task is still open: commit after done or give_up'}

        with self._grading:
            if op == 'commit':
                return self._commit(run_id, '')

            graded = str(message[what]) if what == 'claim' else message[what]

            try:
                store.stage(run_id, graded, str(message['grade']), str(message.get('reason', '')).strip())
            except KnowledgeError as error:
                return {'ok': False, 'message': str(error)}

        said = f'{what} {graded} staged: {message["grade"]}; agent_cli commit after the task ends'
        self.tasks.tracer.emit('knowledge', said, grade=message['grade'], run_id=run_id, **{what: graded})

        return {'ok': True, 'message': said}

    def _stores(self) -> list[StagedRuns]:
        """The stores that keep staged grades: the claims' and the steps'"""
        return [store for store in (self.knowledge, self.steps) if store is not None]

    def _commit(self, run_id: str, when: str) -> dict:
        """The staged grades of a run go in, each store alone: a file that cannot be committed is set aside, and the
        other store's grades still go in. The caller holds _grading"""
        said, ungraded, failed = [], [], []

        for store in self._stores():
            try:
                if not store.has_staged(run_id):
                    continue

                kept, left = store.commit(run_id)
                said      += kept
                ungraded  += left
            except Exception as error:                   # bookkeeping must never stop a task from opening
                failed.append(f'{store.staged_dir.name}/{run_id}: {error!r}, set aside')
                self._set_aside(store, run_id)

        self.tasks.tracer.emit('knowledge', f'committed {run_id}{when}: ' + ('; '.join(said) or 'no grades'),
                               run_id=run_id, grades=said)

        for problem in failed:
            self.tasks.tracer.emit('knowledge', f'could not commit {problem}', level='warn', run_id=run_id)

        return {'ok': not failed, 'message': '\n'.join(said + [f'could not commit {problem}' for problem in failed])
                                             or 'no grades to commit', **({'not_graded': ungraded} if ungraded else {})}

    def _commit_left(self) -> None:
        """At a task open: the grades that an earlier task staged and did not commit go in first"""
        with self._grading:
            for run_id in sorted({run_id for store in self._stores() for run_id in self._staged_runs(store)}):
                self._commit(run_id, ' at the next open')

    @staticmethod
    def _staged_runs(store) -> list[str]:
        try:
            return store.staged_runs()
        except OSError:
            return []

    def _set_aside(self, store: StagedRuns, run_id: str) -> None:
        try:
            store.set_aside(run_id)
        except (OSError, KnowledgeError) as error:
            self.tasks.tracer.emit('knowledge', f'could not set {run_id} aside: {error!r}', level='warn', run_id=run_id)

    def _stale_now(self, about: str) -> dict | None:
        """The stale skills, read once for a step or the catalog; None when they cannot be read, and the trace says so"""
        try:
            return self.stale()
        except OSError as error:
            self.tasks.tracer.emit('stale_skill', f'{about}: could not be checked: {error!r}', level='warn')
            return None

    def _keep(self, skill: str, run_id: str) -> None:
        """Keep that the skill ran. The step HAS happened: a failure to write it down must not be reported as a
        failed command, or the agent runs the action again (command() turns an OSError into ok: false)"""
        try:
            self.ran(skill, run_id)
        except OSError as error:
            self.tasks.tracer.emit('stamp_error', f'{skill}: the run was not kept: {error!r}', level='warn')

    @staticmethod
    def _stale_reason(skill: str, parts: list[str], stale: dict | None) -> str:
        """Why the skill is stale: its own reason, or that of a part at any depth. '' when the stale skills are not
        known (None)"""
        if not stale:
            return ''

        if skill in stale:
            return stale[skill]

        return next((f'its part {part} is stale: {stale[part]}' for part in parts if part in stale), '')

    def _trust(self, skill: str, stale: dict | None, seen: dict | None = None) -> dict:
        """What run time does with the skill. seen: its composition, when the caller read it already.
          maturity    graduation.at_run_time, where a stale part makes it stale too
          earned      it and every part earned tested on the robot: only that, not a contract's word, passes the gate
                      with no question
          deprecated  the skill, or a part, that run refuses
          stale       why it is stale
        A file that cannot be read gives untested, so the gate asks, and the trace says why: bookkeeping never stops a
        step"""
        found = {'maturity': 'untested', 'earned': False, 'deprecated': '', 'stale': ''}

        if (contract := self.tasks.registry.get(skill)) is None:
            return found

        try:
            seen    = self.composition(skill) if seen is None else seen
            parts   = seen.get('calls', [])
            unknown = bool(seen.get('unknown'))
            graded = {name: self._graded(name) for name in [skill, *parts]}
            reason = self._stale_reason(skill, parts, stale)
        except Exception as error:                       # a file that cannot be read, or decoded
            self.tasks.tracer.emit('maturity', f'{skill}: maturity not read, so it counts as untested: {error!r}',
                                   level='warn', skill=skill)
            return found

        return found | {
            'maturity':   at_run_time(graded[skill], contract.maturity, stale is None or bool(reason)),
            # a call the code does not name can be any skill: grades of the code cannot vouch for it
            'earned':     all(entry and entry['earned'] == 'tested' for entry in graded.values()) and not unknown,
            'deprecated': next((name for name, entry in graded.items() if entry and entry['maturity'] == 'deprecated'), ''),
            'stale':      reason,
        }

    def _graded(self, skill: str) -> dict | None:
        """Graduation.of the skill; None when the library does not have it, or nothing reads grades"""
        contract = self.tasks.registry.get(skill)
        return self.graduation.of(skill, contract.maturity) if contract and self.graduation else None

    def ask_step(self, request: dict, still_open=None, decided: dict | None = None) -> dict:
        """Ask a person about one step and trace the answer. A code skill or a tree is one step: its parts never ask.
        decided: an answer known already (TRUSTED), traced the same way, with no question"""
        if self.gate is None:
            return {'approve': True, 'state': 'approved', 'reason': ''}

        task     = self.task
        full     = {'run_id': task.run_id if task else None, 'step': task.steps + 1 if task else 0} | request
        decision = decided or self.gate.ask(full, still_open or (lambda: self.task is task))
        self.tasks.tracer.emit('step_gate', f'{request.get("skill")}: {decision["state"]} {decision["reason"]}'.strip(),
                               skill=request.get('skill'), subgoal=request.get('subgoal', ''), step=full['step'],
                               run_id=full['run_id'], **{key: request[key] for key in ('end',) if key in request},
                               **decision)   # its own run: a late refusal of a closed task; end: not a step

        return decision

    def _ask_end(self, task: Task, op: str, reason: str) -> dict:
        """done or give_up. With the gate on, a person approves the end first, or refuses it with a reason and the task
        stays open: on 2026-11-20 run 25bd4daf gave up in front of the call button, and nobody could say "look at the
        door frames". The time limit ends a task with no question, and so does the end of a task that cannot run a
        step: one at max_steps, or a discovery task, where nobody may watch"""
        end = 'done' if op == 'done' else 'gave_up'

        if task.steps >= self.limits['max_steps'] or not self.limits['run']:
            return self._end(task, end, reason)

        decision = self.ask_step({'skill': op, 'end': end, 'subgoal': reason})   # no gate: approved

        if self.task is not task:                        # it ended while the person decided: the time limit, a cancel
            return _refused('the task ended while the end waited at the gate', decision['reason'],
                            decision.get('by', EXECUTOR))

        if decision.get('by') == EXECUTOR:              # no person saw it: a request waits at the gate already
            return _refused(f'the end did not go to the operator: {decision["reason"]}. Wait, then send it again',
                            decision['reason'], EXECUTOR)

        if not decision['approve']:
            why = decision['reason'] or 'no reason given'
            return _refused(f'the operator refused the end: {why}. The task is still open: continue the task', why,
                            decision.get('by', EXECUTOR))

        return self._end(task, end, reason)

    def _end(self, task: Task, end: str, reason: str) -> dict:
        if not self._close(task, end):                   # first: else check() sees it end and closes it as 'ended'
            return {'ok': False, 'message': 'the task ended already'}   # a time limit at the same time: end it once

        self.tasks.end_task(task, end, reason)
        message  = f'the task ended: {end}'

        staged   = self._staged(task.run_id)

        if staged:                                       # said, not enforced: a grade is the agent's judgement
            ungraded = [claim_id for claim_id, entry in staged.items() if not entry['grade']]
            message += '; grade what is left, then agent_cli commit' + (f'; not graded: {", ".join(ungraded)}' if ungraded else '')

        return {'ok': True, 'message': message}

    def _staged(self, run_id: str) -> dict:
        """What the run staged in each store, by what the agent grades: a claim id, or 'step <n>'"""
        staged = {}

        for store in self._stores():
            try:
                staged |= store.staged(run_id)
            except (OSError, ValueError, KnowledgeError):
                continue

        return staged

    def _close(self, task: Task, end: str) -> bool:
        """Close the task; False when it was closed already"""
        with self._lock:                                 # check() and a command can both close it; a new task may be open
            if self.task is not task:
                return False

            self.task, self.ended = None, task.run_id

        if self.gate is not None:
            self.gate.cancel('the task ended')

        self.publish_open({'state': 'closed', 'run_id': task.run_id, 'end': end})

        return True
