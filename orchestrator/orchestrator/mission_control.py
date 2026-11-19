"""Mission Control's adapter, without ROS: the state the page shows, and what a click does.

mission_control_node.py wires the ROS topics, the web server and the files. This module holds the state of a run
and the rules of a command, so that both can be read and tested alone.

The page reads one state message (GET /api/state) and sends a task, a step decision or a cancel (POST). A
decision goes out on the same topic as `agent_cli step`, so the gate, the limits and the recorder see no
difference. The step gate itself is the only judge of a decision (StepGate.decide in step_gate.py): this module
checks the HTTP body, sends the decision and relays the gate's answer.
"""

import hmac
import time
import threading

from collections import deque
from pathlib import Path
from typing import Callable

from skill_registry import missing_folder
from skill_registry.deny import DEFAULT_PATH as DENY_FILE
from skill_registry.skill_review import ReviewError, keep, library, remove

from .images import Camera, Outlines
from .topics import TASK_OPTIONS


MAX_TASK_CHARS = 400                    # a task sentence; longer is a plan, not a task
TRACE_KEEP     = 400                    # trace events the page can scroll back through
DECISION_WAIT  = 5.0                    # seconds to wait for the executor's reply to a decision
TASK_FIELDS    = ('state', 'run_id', 'task', 'blind', 'discovery', 'end', 'limits')   # of the open message; not places, known or scratch
STEPS_KEEP     = 200                    # steps of one task the page shows in the plan
STEP_ENDS      = ('finished', 'halted', 'error')


class MissionState:
    """The latest message of each topic the page shows, and the last trace events with a cursor.

    The ROS callbacks write it on the spin thread; the web server reads it on its own threads.
    """

    def __init__(self, keep: int = TRACE_KEEP):
        self._lock   = threading.Lock()
        self._parts  = {}                            # status, task, step, recorder: the latest message of each
        self._plan   = {}                            # the newest plan of the open task, from the trace
        self._steps  = deque(maxlen=STEPS_KEEP)      # the steps of the open task, from the trace: run, ended or refused
        self._began  = None                          # when the open task started, from the trace
        self._run    = None                          # the run that the plan, the steps and began belong to
        self._events = deque(maxlen=keep)
        self._seq    = 0

    def put(self, part: str, message: dict):
        """The latest message of one part; a new task forgets the plan and the steps of the last one"""
        with self._lock:
            if part == 'task':
                self._new_run(message.get('run_id'))

            self._parts[part] = message

    def _new_run(self, run_id):
        """The open message and task_started come on two topics, in either order: the first of them starts the run"""
        if run_id != self._run:
            self._run, self._plan, self._began = run_id, {}, None
            self._steps.clear()

    def trace(self, event: dict):
        """One /orchestrator/trace event; a plan event is also kept as the plan, and a step event as a step"""
        with self._lock:
            self._seq   += 1
            event['seq'] = self._seq
            self._events.append(event)
            kind         = event.get('kind')

            if kind == 'task_started':
                self._new_run(event.get('run_id'))
                self._began = event.get('time')
                return

            if event.get('run_id') != self._run:     # a late event of a closed task
                return

            if kind == 'plan' and event.get('subgoals'):
                self._plan = {'version': event.get('version'), 'subgoals': list(event['subgoals'])}
            elif kind == 'started':
                self._steps.append({'step': event.get('step'), 'skill': event.get('tree_id'), 'subgoal': event.get('subgoal') or '',
                                    'status': 'running', 'start': event.get('time')})
            elif kind in STEP_ENDS and self._steps and self._steps[-1]['status'] == 'running':
                self._steps[-1].update(status=event.get('status') if kind == 'finished' else kind, end=event.get('time'))
            elif kind == 'step_gate' and event.get('state') == 'refused' and isinstance(event.get('step'), int):
                # not a refusal inside a code skill (step 'inside the code'): that one comes while its step runs
                self._steps.append({'step': event.get('step'), 'skill': event.get('skill'), 'subgoal': event.get('subgoal') or '',
                                    'status': 'refused', 'reason': event.get('reason', ''), 'start': event.get('time'),
                                    'end': event.get('time')})

    def snapshot(self, after: int = 0) -> dict:
        """Everything the page shows; only the trace events newer than after. The task's open message is cut down
        to TASK_FIELDS, because its places and what the knowledge store knows would be resent at every poll"""
        with self._lock:
            task = self._parts.get('task', {})

            return {
                'time':   time.time(),
                'seq':    self._seq,
                'keep':   self._events.maxlen,
                'plan':   self._plan,
                'steps':  [dict(step) for step in self._steps],
                'began':  self._began,
                'trace':  [] if after >= self._seq else [event for event in self._events if event['seq'] > after],
                'task':   {field: task[field] for field in TASK_FIELDS if field in task},
                **{part: self._parts.get(part, {}) for part in ('status', 'step', 'recorder')},
            }


def token_ok(expected: str, given: str) -> bool:
    """True when a command carries the node's token. `off` takes the check away, for a bench with no agent on it"""
    return expected == 'off' or (bool(expected) and hmac.compare_digest(expected.encode(), (given or '').encode()))


class Commands:
    """What a click does. Every command answers (code, payload)."""

    def __init__(self, send_task: Callable[[dict], bool], decide_step: Callable[[dict], dict | None],
                 send_cancel: Callable[[], bool]):
        self.send_task   = send_task
        self.decide_step = decide_step               # publishes the decision and waits for the executor's reply
        self.send_cancel = send_cancel

    def task(self, body: dict) -> tuple[int, dict]:
        sentence = ' '.join(str(body.get('task') or '').split())

        if not sentence:
            return 400, {'ok': False, 'message': 'a task needs a sentence'}

        if len(sentence) > MAX_TASK_CHARS:
            return 400, {'ok': False, 'message': f'a task sentence is {MAX_TASK_CHARS} characters at most, not {len(sentence)}'}

        options = {option: bool(body.get(option)) for option in TASK_OPTIONS}

        if not self.send_task({'task': sentence, **options}):
            return 504, {'ok': False, 'message': 'the executor does not listen for a task; is it running?'}

        return 200, {'ok': True, 'message': ' '.join(['task sent', *(f'({option})' for option, on in options.items() if on)])}

    def step(self, body: dict) -> tuple[int, dict]:
        """Approve or refuse a step. The gate takes or refuses the decision; this only relays its answer"""
        approve = body.get('approve')
        step_id = str(body.get('id') or '')

        if not isinstance(approve, bool):
            return 400, {'ok': False, 'message': 'a decision needs approve: true or approve: false'}

        if not step_id:
            return 400, {'ok': False, 'message': 'a decision names the step it is for'}

        reply = self.decide_step({'id': step_id, 'approve': approve, 'reason': str(body.get('reason') or ''),
                                  'by': 'mission_control'})

        if reply is None:
            return 504, {'ok': False, 'message': 'the executor did not take the decision; is it running?'}

        accepted = bool(reply.get('accepted'))

        return (200 if accepted else 409), {'ok': accepted, 'message': reply.get('message', '')}

    def cancel(self, body: dict) -> tuple[int, dict]:
        if not self.send_cancel():
            return 504, {'ok': False, 'message': 'the executor does not listen for a cancel; is it running?'}

        return 200, {'ok': True, 'message': 'cancel sent'}


class SkillShelf:
    """The Skills tab: the library as files, and a person's keep or remove of a skill the agent wrote (skill_review).
    The executor's registry sees each change at its next refresh"""

    def __init__(self, directory, stamps_file, removed_dir):
        self.directory   = Path(directory)
        self.stamps_file = stamps_file
        self.removed_dir = Path(removed_dir)

    def get(self) -> tuple[int, dict]:
        try:
            deny = DENY_FILE.read_text()
        except OSError as error:
            deny = f'(cannot read {DENY_FILE}: {error})'

        try:
            skills = library(self.directory, self.stamps_file)
        except OSError as error:                    # e.g. a folder moved between the listing and the read
            return 500, {'ok': False, 'message': f'cannot read the library: {error}'}

        return 200, {'skills': skills, 'problem': missing_folder(self.directory), 'deny': deny,
                     'directory': str(self.directory)}

    def keep(self, body: dict) -> tuple[int, dict]:
        return self._review(lambda name, version: keep(self.directory, name, self.stamps_file, version), body)

    def remove(self, body: dict) -> tuple[int, dict]:
        return self._review(lambda name, version: remove(self.directory, name, self.removed_dir, self.stamps_file, version),
                            body)

    def _review(self, action, body: dict) -> tuple[int, dict]:
        """A page names the skill and the version it showed: a newer one is never kept or removed unread"""
        name, version = body.get('name'), body.get('version')

        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            return 400, {'ok': False, 'message': 'name the skill and the version you read'}

        try:
            return 200, {'ok': True, 'message': action(name, version)}
        except ReviewError as error:
            return 409, {'ok': False, 'message': str(error)}
        except OSError as error:
            return 500, {'ok': False, 'message': f'the file change failed: {error}'}


class MissionApi:
    """The paths under /api; the node's web server does the HTTP and the files"""

    def __init__(self, state: MissionState, commands: Commands, camera: Camera | None = None,
                 token_needed: bool = True, outlines: Outlines | None = None, skills: SkillShelf | None = None):
        self.state        = state
        self.camera       = camera or Camera()
        self.outlines     = outlines or Outlines()
        self.token_needed = token_needed     # the page says so when it holds no token; it cannot see the parameter
        self.skills       = skills
        self.posts        = {'/api/task': commands.task, '/api/step': commands.step, '/api/cancel': commands.cancel,
                             **({'/api/skill/keep': skills.keep, '/api/skill/remove': skills.remove} if skills else {})}

    def get(self, path: str, query: dict) -> tuple[int, dict]:
        if path == '/api/skills' and self.skills:
            return self.skills.get()

        if path != '/api/state':
            return 404, {'ok': False, 'message': f'no such path {path}'}

        try:
            after = int(query.get('after', 0))
        except (TypeError, ValueError):
            after = 0

        if query.get('outlines') == '1':            # the page shows the masks: keep the outlines coming
            self.outlines.ask()

        return 200, {**self.state.snapshot(after), 'camera': self.camera.state(), 'outlines': self.outlines.state(),
                     'token_needed': self.token_needed}

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        command = self.posts.get(path)

        if command is None:
            return 404, {'ok': False, 'message': f'no such path {path}'}

        if not isinstance(body, dict):
            return 400, {'ok': False, 'message': 'the body is a JSON object'}

        return command(body)
