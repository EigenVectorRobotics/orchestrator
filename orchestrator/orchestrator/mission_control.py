"""Mission Control's adapter, without ROS: the state the page shows, and what a click does.

mission_control_node.py wires the ROS topics, the web server and the files. This module holds the state of a run
and the rules of a command, so that both can be read and tested alone.

The page reads one state message (GET /api/state) and sends a task, a step decision or a cancel (POST). A
decision goes out on the same topic as `agent_cli step`, so the gate, the limits and the recorder see no
difference. The step gate itself is the only judge of a decision (StepGate.decide in step_gate.py): this module
checks the HTTP body, sends the decision and relays the gate's answer.
"""

import hmac
import json
import math
import time
import threading

from collections import deque
from pathlib import Path
from typing import Callable

import yaml

from skill_registry import missing_folder
from skill_registry.deny import DEFAULT_PATH as DENY_FILE
from skill_registry.skill_review import ReviewError, keep, library, remove
from skill_registry.skill_stamps import SkillClock

from .images import Camera, Outlines
from .knowledge import OPERATOR, Claim, KnowledgeError, KnowledgeStore, Sighting
from .graduation import Graduation
from .map_view import MapView
from .run_browser import list_runs, run_detail
from .session_log import SessionLog
from .topics import TASK_OPTIONS


MAX_TASK_CHARS = 400                    # a task sentence; longer is a plan, not a task
TRACE_KEEP     = 400                    # trace events the page can scroll back through
DECISION_WAIT  = 5.0                    # seconds to wait for the executor's reply to a decision
TASK_FIELDS    = ('state', 'run_id', 'task', 'blind', 'discovery', 'end', 'limits')   # of the open message; not places, known or scratch
STEPS_KEEP     = 200                    # steps of one task the page shows in the plan
STEP_ENDS      = ('finished', 'halted', 'error')
AFTER_CLOSE    = 5.0                    # seconds the agent's session log is still read after its mission closed


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
        self._words  = deque(maxlen=keep)            # what the agent thought and said, from its session log (session_log.py)
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

    def words(self, items: list[dict], run_id):
        """The agent's own words: not trace events, but on the same cursor, so the page reads both with one after"""
        with self._lock:
            for item in items:
                self._seq   += 1
                self._words.append({**item, 'run_id': run_id, 'seq': self._seq})

    def run(self) -> tuple[str, float, bool] | None:
        """(run_id, start time, open) of the last mission, or None before its start"""
        with self._lock:
            if self._run is None or self._began is None:
                return None

            task = self._parts.get('task', {})
            return self._run, self._began, task.get('state') == 'open' and task.get('run_id') == self._run

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
                'words':  [] if after >= self._seq else [word for word in self._words if word['seq'] > after],
                'task':   {field: task[field] for field in TASK_FIELDS if field in task},
                **{part: self._parts.get(part, {}) for part in ('status', 'step', 'recorder')},
            }


class SessionFollower:
    """Put what the agent thought and said since its mission opened in the feed. They do not go on
    /orchestrator/trace: the page shows them, the recorder does not keep them. After the mission closes, it reads
    for AFTER_CLOSE more: the agent's last words come after the last read, or just after the close"""

    def __init__(self, state: MissionState, log: SessionLog, clock=time.monotonic):
        self.state, self.log, self.clock = state, log, clock
        self._closed = {}                        # run_id -> when this saw it closed

    def __call__(self) -> int:
        run = self.state.run()

        if run is None:
            return 0

        run_id, began, open_ = run

        if not open_:
            closed = self._closed.setdefault(run_id, self.clock())

            if len(self._closed) > 1:            # only the last run is still read
                self._closed = {run_id: closed}

            if self.clock() - closed > AFTER_CLOSE:
                return 0

        items = self.log.new(began)
        self.state.words(items, run_id)
        return len(items)


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


def live_names(data: str) -> set[str] | None:
    """registry_node's /skills/live: a JSON list of names, or of {name, ...}. Anything else is None: it says nothing,
    and it never raises on the spin thread"""
    try:
        items = json.loads(data)
    except ValueError:
        return None

    if not isinstance(items, list):
        return None

    names = [item.get('name') if isinstance(item, dict) else item for item in items]

    return set(names) if all(isinstance(name, str) for name in names) else None


class StaleSkills:
    """Which skills are stale and why, as the executor's catalog says (skill_stamps): the stamps and the repo roots
    come from the code clock the executor saves beside the knowledge store. A check runs git in every repo, so the
    answer, or the reason there is none, is kept for every_s, and one request at a time makes it.

    Returns ({skill: why}, '') or (None, why it is not known)"""

    def __init__(self, knowledge_dir, skills_dir, every_s: float = 30.0, clock=time.monotonic):
        self.knowledge_dir = knowledge_dir
        self.skills_dir    = Path(skills_dir).expanduser()
        self.every_s       = every_s
        self.clock         = clock
        self._said         = (-math.inf, (None, ''))
        self._lock         = threading.Lock()

    def __call__(self) -> tuple[dict | None, str]:
        with self._lock:
            if self.clock() - self._said[0] >= self.every_s:
                self._said = (self.clock(), self._check())

            return self._said[1]

    def _check(self) -> tuple[dict | None, str]:
        code = SkillClock.load(self.knowledge_dir)

        if code is None:
            return None, 'stale not known: no executor has saved its code clock here yet'

        if code.stamps.skills.resolve() != self.skills_dir.resolve():
            return None, f'stale not known: the executor stamps another library ({code.stamps.skills})'

        try:
            return code.stamps.reasons(code.registry), ''
        except Exception as error:                  # a bad stamps file: said, and the library still shows
            return None, f'stale not known: {error!r}'


class SkillShelf:
    """The Skills tab: the library as files, and a person's keep or remove of a skill the agent wrote (skill_review).
    The executor's registry sees each change at its next refresh. stale: {skill: why}; live: the skills whose
    interface is in the ROS graph now (registry_node's /skills/live), or None while nothing says"""

    def __init__(self, directory, stamps_file, removed_dir, stale: Callable[[], tuple] = lambda: (None, ''),
                 live: Callable[[], set | None] = lambda: None, graduation: Graduation | None = None):
        self.directory   = Path(directory)
        self.stamps_file = stamps_file
        self.removed_dir = Path(removed_dir)
        self.stale       = stale
        self.live        = live
        self.graduation  = graduation          # the maturity each skill earned from the grades of its steps

    def get(self) -> tuple[int, dict]:
        try:
            deny = DENY_FILE.read_text()
        except OSError as error:
            deny = f'(cannot read {DENY_FILE}: {error})'

        try:
            skills = library(self.directory, self.stamps_file)
        except OSError as error:                    # e.g. a folder moved between the listing and the read
            return 500, {'ok': False, 'message': f'cannot read the library: {error}'}

        stale, unknown = self.stale()
        live           = self.live()

        for item in skills:
            item['stale'] = None if stale is None else stale.get(item['name'], '')   # None: not known
            item['live']  = None if live is None else item['name'] in live

            if not item['error']:
                item['earned']   = self.graduation.of(item['name'], item['contract']['maturity']) if self.graduation else None
                item['maturity'] = item['earned']['maturity'] if item['earned'] else item['contract']['maturity']

        return 200, {'skills': skills, 'problem': missing_folder(self.directory), 'stale_unknown': unknown, 'deny': deny,
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


VIA = 'mission_control'                 # in the knowledge log: the operator acted on this page, not at a terminal


def _sighting(sighting: Sighting, kind: str) -> dict:
    return {'kind': kind, 'by': sighting.by, 'time': sighting.time, 'run': sighting.run, 'evidence': sighting.evidence,
            'value': sighting.value, 'moved': sighting.moved, 'grade': sighting.grade, 'reason': sighting.reason}


def _claim(claim: Claim) -> dict:
    """What the page shows of a claim; the counts are over the sightings on the code that is here now"""
    counted = claim.counted

    return {'id': claim.id, 'text': claim.text, 'tier': claim.tier, 'about': claim.about, 'key': claim.key,
            'value': claim.value, 'approved_by': claim.approved_by, 'valid_for_s': claim.valid_for_s,
            'reliability': round(claim.reliability(), 3), 'ready': claim.ready_for_fact(),
            'confirmed': len(counted.confirmations), 'contradicted': len(counted.contradictions),
            'last_seen': claim.last_seen if claim.sightings else None, 'stats': claim.value_stats(),
            'sightings': sorted([*map(lambda s: _sighting(s, 'confirm'), claim.confirmations),
                                 *map(lambda s: _sighting(s, 'contradict'), claim.contradictions),
                                 *map(lambda s: _sighting(s, 'grade'), claim.grades)], key=lambda s: -s['time'])}


class KnowledgeShelf:
    """The Knowledge tab: the claims, the staged grades of the runs (to read: the agent commits them, or the next
    task open does), and the contradictions not seen yet; the operator's approve, retire and seen. The store's file
    lock is shared with the executor and the CLI; the executor's code clock comes from the file it saves, as for the
    knowledge CLI"""

    EXPIRE_S = 30.0                                 # expiry and code moves are applied before a read, this often
    RECENT_S = 14 * 24 * 3600.0                     # "contradicted lately": this far back

    def __init__(self, folder, clock=time.time):
        self.folder   = Path(folder)
        self.clock    = clock
        self._expired = -math.inf                   # when expiry and code moves were last applied
        self._contra  = (None, [])                  # (the log's size and time, what contradicted() said)

    def _store(self, code: bool = True) -> KnowledgeStore:
        return KnowledgeStore(self.folder, code=SkillClock.load(self.folder) if code else None)

    def get(self) -> tuple[int, dict]:
        try:
            if self.clock() - self._expired > self.EXPIRE_S:       # as the CLI does before a read: the tiers are now
                self._store().expire()
                self._expired = self.clock()

            store  = self._store(code=False)
            staged = [{'run': run, 'grades': [{'claim': claim_id, **entry} for claim_id, entry in store.staged(run).items()]}
                      for run in store.staged_runs()]

            return 200, {'claims': [_claim(claim) for claim in store.claims()], 'staged': staged,
                         'contradicted': self._contradicted(store), 'folder': str(self.folder)}
        except (KnowledgeError, OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError) as error:
            return 500, {'ok': False, 'message': f'cannot read the knowledge store: {error!r}'}

    def _contradicted(self, store: KnowledgeStore) -> list[dict]:
        """Read the log again only when it changed: it grows with every sighting"""
        try:
            stat = store.log_path.stat()
            key  = (stat.st_size, stat.st_mtime_ns, store.claims_path.stat().st_mtime_ns if store.claims_path.exists() else 0)
        except FileNotFoundError:
            return []

        if self._contra[0] != key:
            self._contra = (key, store.contradicted(since=self.clock() - self.RECENT_S))

        return self._contra[1]

    def approve(self, body: dict) -> tuple[int, dict]:
        text = body.get('text')

        if text is not None and (not isinstance(text, str) or not text.strip()):
            return 400, {'ok': False, 'message': 'a fact needs its text'}

        return self._act(body, lambda store, claim_id: store.approve(claim_id, OPERATOR, text, via=VIA).id, 'approved ')

    def retire(self, body: dict) -> tuple[int, dict]:
        reason = str(body.get('reason') or 'retired on Mission Control')

        return self._act(body, lambda store, claim_id: store.retire(claim_id, OPERATOR, reason, via=VIA) or claim_id,
                         'retired ')

    def seen(self, body: dict) -> tuple[int, dict]:
        return self._act(body, lambda store, claim_id: store.seen(claim_id, OPERATOR, via=VIA) or claim_id, 'seen ')

    def _act(self, body: dict, action, said: str) -> tuple[int, dict]:
        claim_id = body.get('id')

        if not isinstance(claim_id, str) or not claim_id:
            return 400, {'ok': False, 'message': 'name the claim'}

        try:
            return 200, {'ok': True, 'message': said + action(self._store(), claim_id)}
        except KnowledgeError as error:
            return 409, {'ok': False, 'message': str(error)}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError) as error:
            return 500, {'ok': False, 'message': f'the knowledge store could not be changed: {error!r}'}


class RunShelf:
    """The Runs tab: the recorder's run bundles, to read (run_browser)"""

    def __init__(self, root):
        self.root = Path(root).expanduser()

    def list(self) -> tuple[int, dict]:
        try:
            return 200, {'runs': list_runs(self.root), 'root': str(self.root)}
        except OSError as error:
            return 500, {'ok': False, 'message': f'cannot read {self.root}: {error!r}'}

    def get(self, key) -> tuple[int, dict]:
        if not isinstance(key, str) or not key:
            return 400, {'ok': False, 'message': 'name the run'}

        try:
            detail = run_detail(self.root, key)
        except OSError as error:
            return 500, {'ok': False, 'message': f'cannot read the run: {error!r}'}

        return (200, detail) if detail else (404, {'ok': False, 'message': f'no run {key}'})


class MissionApi:
    """The paths under /api; the node's web server does the HTTP and the files"""

    def __init__(self, state: MissionState, commands: Commands, camera: Camera | None = None,
                 token_needed: bool = True, outlines: Outlines | None = None, skills: SkillShelf | None = None,
                 knowledge: KnowledgeShelf | None = None, runs: RunShelf | None = None, map_view: MapView | None = None,
                 session_log: SessionLog | None = None):
        self.state        = state
        self.camera       = camera or Camera()
        self.outlines     = outlines or Outlines()
        self.token_needed = token_needed     # the page says so when it holds no token; it cannot see the parameter
        self.skills       = skills
        self.knowledge    = knowledge
        self.runs         = runs
        self.map_view     = map_view or MapView()
        self.session_log  = session_log
        self.posts        = {'/api/task': commands.task, '/api/step': commands.step, '/api/cancel': commands.cancel,
                             **({'/api/skill/keep': skills.keep, '/api/skill/remove': skills.remove} if skills else {}),
                             **({'/api/knowledge/approve': knowledge.approve, '/api/knowledge/retire': knowledge.retire,
                                 '/api/knowledge/seen': knowledge.seen} if knowledge else {})}

    def get(self, path: str, query: dict) -> tuple[int, dict]:
        if path == '/api/skills' and self.skills:
            return self.skills.get()

        if path == '/api/knowledge' and self.knowledge:
            return self.knowledge.get()

        if path == '/api/runs' and self.runs:
            return self.runs.list()

        if path == '/api/run' and self.runs:
            return self.runs.get(query.get('key'))

        if path != '/api/state':
            return 404, {'ok': False, 'message': f'no such path {path}'}

        try:
            after = int(query.get('after', 0))
        except (TypeError, ValueError):
            after = 0

        if query.get('outlines') == '1':            # the page shows the masks: keep the outlines coming
            self.outlines.ask()

        return 200, {**self.state.snapshot(after), 'camera': self.camera.state(), 'outlines': self.outlines.state(),
                     'map': self.map_view.state(),
                     'reasoning': self.session_log.status() if self.session_log else None,
                     'token_needed': self.token_needed}

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        command = self.posts.get(path)

        if command is None:
            return 404, {'ok': False, 'message': f'no such path {path}'}

        if not isinstance(body, dict):
            return 400, {'ok': False, 'message': 'the body is a JSON object'}

        if path.startswith('/api/knowledge/') and not self.token_needed:
            # an act as the operator: with the token off, any process on this host (the agent's too) could do it
            return 403, {'ok': False, 'message': 'approve, retire and seen need the page token, and it is off'}

        return command(body)
