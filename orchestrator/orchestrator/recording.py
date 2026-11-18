"""Run bundles on the NUC: one folder per recorder session, one per tree run, for review after the runs.

A session folder holds provenance.json, every trace event in events.jsonl and finalization.json. Each tree run
(the executor's `started` to `finished`, `halted` or `error`) gets runs/<n>-<tree>/ with its tree, its records,
its agent calls, the press frames it saved, its bag, and outcome.json. The recorder is the only writer here.

Each agent call gets runs/<n>-<tree>/agent/<id>/:
  request.json    what the model got: task, allowed decisions, failure, history, press, stance, and its images by file
                  (written when the call opens, replaced when it closes, with its outcome)
  messages.jsonl  everything that happened, in order: opened, started (model, effort, prompt, system prompt and its
                  sha256, the sha256 of agent_prompts.py), each decision with the executor's reply and the model's
                  usage, failed, cancelled, closed
  summary.json    one record of the call: model, prompt, the two sha256s, final decision, state, time, tokens and cost in all
The images it names are copied to the run's captures/ with the press frames.

The press frames are copied from the capture directory when the run ends, and the originals are then removed:
the capture directory is a buffer, the run folder keeps them. Old runs are cleared by hand (see `largest_runs`).

This module needs no ROS: run_recorder feeds it messages and starts and stops the bag.
"""

import json
import time
import uuid
import shutil
import hashlib
import subprocess

from pathlib import Path
from datetime import UTC, datetime
from operator import itemgetter
from dataclasses import dataclass

from .press_capture import DEFAULT_CAPTURE_DIR


RUN_END_KINDS    = ('finished', 'halted', 'error')
PREFLIGHT_TREE   = 'preflight'                  # the BehaviorTree ID of skill_registry/preflight/preflight.xml
MAX_HASHED_BYTES = 5_000_000                   # an untracked file bigger than this is hashed by name and size


def write_json(path: Path, value):
    """Write atomically: a reader sees the old file or the new one, never half of it"""
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=str) + '\n')
    temporary.replace(path)


def file_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else None


def directory_digests(directory: Path, pattern: str) -> dict:
    """File name -> hash of its bytes, for the contracts and the agent skill documents"""
    return {path.name: file_digest(path) for path in sorted(Path(directory).glob(pattern))}


def git_state(repo: Path) -> dict:
    """Revision, branch, and a hash of the local edits (tracked diff and untracked files), so a run with
    uncommitted changes is not mistaken for its commit"""
    def git(*arguments):
        result = subprocess.run(['git', '-C', str(repo), *arguments], capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else None

    names = git('rev-parse', 'HEAD', '--abbrev-ref', 'HEAD')

    if names is None:
        return {'path': str(repo), 'error': 'not a git checkout'}

    revision, branch = names.decode().split()
    edits            = git('diff', 'HEAD') or b''

    for name in filter(None, (git('ls-files', '--others', '--exclude-standard', '-z') or b'').split(b'\0')):
        path   = repo / name.decode()
        size   = path.stat().st_size if path.is_file() else 0
        edits += name + (path.read_bytes() if size <= MAX_HASHED_BYTES else str(size).encode())

    return {
        'path':     str(repo),
        'revision': revision,
        'branch':   branch,
        'dirty':    bool(edits),
        'edits':    hashlib.sha256(edits).hexdigest()[:16] if edits else None,
    }


def find_repos(roots: list[str]) -> list[Path]:
    """Each root that is a git checkout, and each checkout directly inside a root (a colcon src/)"""
    repos = []

    for root in roots:
        root = Path(root).expanduser()

        if (root / '.git').exists():
            repos.append(root)
        elif root.is_dir():
            repos += sorted(child for child in root.iterdir() if (child / '.git').exists())

    return repos


def free_gb(path: Path) -> float:
    path = Path(path)

    while not path.exists():                     # the root may not exist yet
        path = path.parent

    return shutil.disk_usage(path).free / 1e9


def largest_runs(root: Path, count: int = 10) -> list[tuple[str, float]]:
    """The biggest run folders under root, in GB, biggest first: what the operator may clear"""
    sizes = []

    for run in Path(root).glob('*/runs/*'):
        size = sum(path.stat().st_size for path in run.rglob('*') if path.is_file())
        sizes.append((str(run.relative_to(root)), size / 1e9))

    return sorted(sizes, key=itemgetter(1), reverse=True)[:count]


def saved_files(value, prefix: str) -> set[str]:
    """Paths under prefix anywhere in a JSON value: the press frames a record or an agent call names"""
    if isinstance(value, str):
        return {value} if value.startswith(prefix) else set()

    if isinstance(value, dict):
        value = list(value.values())
    elif not isinstance(value, list):
        return set()

    return set().union(*(saved_files(item, prefix) for item in value))


STATES = {'answered': 'answered', 'failed': 'failed', 'cancelled': 'failed'}


def summarize_call(call: str, folder: Path) -> dict:
    """One record of an agent call, from its request and messages: what the Run Viewer shows"""
    request  = json.loads((folder / 'request.json').read_text()) if (folder / 'request.json').is_file() else {}
    messages = [json.loads(line) for line in (folder / 'messages.jsonl').read_text().splitlines() if line.strip()] \
        if (folder / 'messages.jsonl').is_file() else []
    started  = next((message for message in messages if message['kind'] == 'started'), {})
    answers  = [message for message in messages if message['kind'] == 'decision']
    accepted = [message for message in answers if message.get('accepted')]
    failure  = next((message for message in messages if message['kind'] in ('failed', 'cancelled')), None)
    closed   = next((message for message in messages if message['kind'] == 'closed'), None)
    opened   = request.get('opened') or (messages[0]['time'] if messages else None)
    state    = STATES.get((closed or {}).get('outcome'), 'failed') if closed else ('failed' if failure else 'thinking')
    totals   = {key: round(sum(message.get(key) or 0 for message in answers + ([failure] if failure else [])), 4)
                for key in ('cost_usd', 'tokens_in', 'tokens_out', 'model_s')}

    return {
        'call':       call,
        'skill':      request.get('skill'),
        'phase':      request.get('phase'),
        'model':      started.get('model'),
        'effort':     started.get('effort'),
        'prompt':     started.get('prompt'),
        'system_sha256':  started.get('system_sha256'),
        'prompts_sha256': started.get('prompts_sha256'),
        'decision':   (accepted or answers or [{}])[-1].get('decision'),
        'answers':    len(answers),
        'refused':    len(answers) - len(accepted),
        'state':      state,
        'error':      (closed or {}).get('error') or (failure or {}).get('error'),
        'started':    round(opened, 1) if opened else None,
        'seconds':    round(((closed or messages[-1])['time'] if messages else opened) - opened, 1) if opened else None,
        'bridge_received': started.get('received'),
        **totals,
    }


@dataclass
class RecorderConfig:
    root:        str   = str(Path.home() / 'orch_runs')
    min_free_gb: float = 20.0
    capture_dir: str   = DEFAULT_CAPTURE_DIR


class NoBag:
    """A bag that records nothing; run_recorder gives a RosBag with the same two methods"""

    def start(self, folder: Path):
        pass

    def stop(self):
        pass


class Recorder:
    def __init__(self, config: RecorderConfig, provenance: dict, bag=None, clock=time.time):
        self.config   = config
        self.bag      = bag or NoBag()
        self.clock    = clock
        self.run      = None                     # the open run's folder
        self.runs     = 0
        self.task     = None                     # the last task text, for the next run
        self.captures = set()                    # the press frames the open run named
        self.calls    = {}                       # agent call id -> its run's folder, for messages after the run ended
        self.failure  = None

        stamp     = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
        self.path = Path(config.root).expanduser() / f'{stamp}-{uuid.uuid4().hex[:6]}'
        self.path.mkdir(parents=True, exist_ok=False)

        write_json(self.path / 'provenance.json', {'schema_version': 1, 'created_utc': stamp, **provenance})
        self._events = (self.path / 'events.jsonl').open('a', buffering=1)
        self._finalize('open')

    # ========== Space ==========

    def free_gb(self) -> float:
        return free_gb(self.path)

    def ready(self) -> tuple[bool, str]:
        """Enough free space for a run, and no write has failed"""
        if self.failure:
            return False, f'a write failed: {self.failure}'

        free = self.free_gb()

        if free < self.config.min_free_gb:
            return False, f'{free:.1f} GB free under {self.path.parent}, below min_free_gb {self.config.min_free_gb}'

        return True, f'{free:.1f} GB free'

    def check_bag_space(self):
        """While a bag records: stop it below a quarter of min_free_gb; the events go on"""
        if self.run is None or self.free_gb() >= self.config.min_free_gb / 4:
            return

        self.bag.stop()
        self._safely((self.run / 'bag_truncated').write_text, f'{self.free_gb():.1f} GB free at {self.clock()}\n')

    # ========== Messages in ==========

    def on_task(self, text: str):
        self.task = {'task': text, 'time': self.clock()}

    def on_event(self, event: dict):
        """A trace event: kept in events.jsonl; the executor's events open and close runs"""
        self._safely(self._on_event, event)

    def _on_event(self, event: dict):
        self._events.write(json.dumps({**event, 'received': self.clock()}, default=str) + '\n')

        if event.get('source') != 'executor':
            return

        kind = event.get('kind')

        if kind in RUN_END_KINDS:
            self._end_run(event)
        elif kind == 'started':
            self._start_run(event)
        elif kind == 'record' and self.run is not None:
            self._append(self.run / 'records.jsonl', event.get('record', {}))
            self._note_captures(event.get('record'))

    def on_agent_request(self, request: dict):
        """An agent call, as the executor opens and closes it; the closed one replaces the open one, also when
        it comes after its run ended"""
        folder = self._agent_folder(request.get('id'))

        if folder is None:
            return

        closed = request.get('state') == 'closed'
        self._safely(write_json, folder / 'request.json', request)
        self._note_captures(request)
        self._agent_message(folder, request['id'], {
            'kind': 'closed' if closed else 'opened',
            'time': request.get('closed' if closed else 'opened', self.clock()),
            **({'outcome': request.get('outcome'), 'error': request.get('error')} if closed else
               {'skill': request.get('skill'), 'phase': request.get('phase')}),
        })

    def on_agent_message(self, kind: str, message: dict):
        """The executor's reply to a decision (kind reply), or an event of the call (kind event: started, decision,
        failed, cancelled)"""
        folder = self._agent_folder(message.get('id'))

        if folder is not None:
            entry = {**message, 'kind': message.get('kind') if kind == 'event' else kind}
            self._agent_message(folder, message['id'], {**entry, 'time': message.get('time', self.clock())})

    def _agent_message(self, folder: Path, call: str, entry: dict):
        self._append(folder / 'messages.jsonl', entry)
        self._safely(self._write_summary, folder, call)

    def _write_summary(self, folder: Path, call: str):
        write_json(folder / 'summary.json', summarize_call(call, folder))

    def close(self):
        self._safely(self._end_run, {'kind': 'closed', 'text': 'the recorder stopped during the run'})
        self._events.close()
        self._finalize('failed' if self.failure else 'closed')

    # ========== Runs ==========

    def _start_run(self, event: dict):
        self._end_run({'kind': 'halted', 'text': 'a new run started before this one ended'})

        self.runs += 1
        tree   = event.get('tree_id') or 'tree'
        folder = self.path / 'runs' / f'{self.runs:03d}-{tree}'
        folder.mkdir(parents=True)
        self.run = folder

        (self.run / 'tree.xml').write_text(event.get('tree', ''))
        write_json(self.run / 'task.json', {
            'run_id':  event.get('run_id'),
            'tree_id': tree,
            'blind':   bool(event.get('blind')),
            'started': event.get('time'),
            'task':    self.task,                 # the planner's last task; none for send_tree
        })

        if tree != PREFLIGHT_TREE:                # the planner sends preflight first: the task is for the tree after it
            self.task = None
        self.bag.start(self.run / 'bag')

    def _end_run(self, event: dict):
        if self.run is None:
            return

        self._safely(self.bag.stop)
        self._copy_captures()
        self._safely(write_json, self.run / 'outcome.json', {
            'end':      event.get('kind'),
            'status':   event.get('status'),
            'aborted':  event.get('aborted'),
            'text':     event.get('text'),
            'finished': event.get('time', self.clock()),
        })
        self.run = None

    def _note_captures(self, value):
        self.captures |= saved_files(value, str(Path(self.config.capture_dir).expanduser()))

    def _copy_captures(self):
        """The press frames this run named, copied in; the originals stay"""
        sources, self.captures = sorted(self.captures), set()

        if not sources:
            return

        folder = self.run / 'captures'

        if not self._safely(folder.mkdir, exist_ok=True):
            return

        for source in map(Path, sources):
            if source.is_file() and self._safely(shutil.copy2, source, folder / source.name):
                self._safely(source.unlink)       # the run keeps it; the capture directory is only a buffer

    def _agent_folder(self, call: str | None) -> Path | None:
        """The agent folder of the run the call opened in; None for a call outside any run"""
        if not call:
            return None

        if call not in self.calls:
            if self.run is None:
                return None

            self.calls[call] = self.run / 'agent' / call

        folder = self.calls[call]

        return folder if self._safely(folder.mkdir, parents=True, exist_ok=True) else None

    # ========== Writes ==========

    def _append(self, path: Path, value: dict):
        def append():
            with path.open('a') as file:
                file.write(json.dumps(value, default=str) + '\n')

        self._safely(append)

    def _safely(self, write, *arguments, **keywords) -> bool:
        """A failed write marks the session failed and returns False; it never stops the node or the tree"""
        try:
            write(*arguments, **keywords)
            return True
        except (OSError, ValueError) as error:
            if self.failure is None:
                self.failure = repr(error)
                self._finalize('failed')

            return False

    def _finalize(self, status: str):
        try:
            write_json(self.path / 'finalization.json', {'status': status, 'failure': self.failure, 'time': self.clock()})
        except OSError:
            pass
