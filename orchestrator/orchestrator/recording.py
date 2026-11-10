"""Run bundles on the NUC: one folder per recorder session, one per task, for review after the runs.

A session folder holds provenance.json, every trace event in events.jsonl and finalization.json. Each task (the
executor's `task_started` to `task_finished`) is one run and gets runs/<n>-<name>/ with its steps (each step's tree
in steps/ and one line per step in steps.jsonl), its records, its agent calls, the press frames it saved, the
agent's scratch folder, its bag, and outcome.json. Nothing is deleted here: the operator clears old runs by hand
(see `largest_runs`).

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


STEP_END_KINDS   = ('finished', 'halted', 'error')
MAX_HASHED_BYTES = 5_000_000                   # an untracked file bigger than this is hashed by name and size


def write_json(path: Path, value):
    """Write atomically: a reader sees the old file or the new one, never half of it"""
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=str) + '\n')
    temporary.replace(path)


def file_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else None


def directory_digests(directory: Path, pattern: str) -> dict:
    """File name -> hash of its bytes, for the contracts and agent skill documents that were loaded"""
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
    def __init__(self, config: RecorderConfig, provenance: dict, bag=None, clock=time.time, library=dict):
        self.config   = config
        self.library  = library       # the skill library now: the agent writes contracts while the session runs
        self.bag      = bag or NoBag()
        self.clock    = clock
        self.run      = None                     # the open run's folder
        self.runs     = 0
        self.task     = None                     # the last task text, for the next run
        self.captures = set()                    # the press frames the open run named
        self.scratch  = None                     # the agent's scratch folder of the open run
        self.step     = None                     # the open step of the open run: its number, tree and start
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

        if kind == 'task_started':
            self._start_run(event)
        elif kind == 'task_finished':
            self._end_run(event)
        elif self.run is None:
            return
        elif kind == 'started':
            self._start_step(event)
        elif kind in STEP_END_KINDS:
            self._end_step(event)
        elif kind == 'plan':
            self._append(self.run / 'plans.jsonl', {'version': event.get('version'), 'subgoals': event.get('subgoals'),
                                                    'time': event.get('time')})
        elif kind == 'record':
            self._append(self.run / 'records.jsonl', event.get('record', {}))
            self._note_captures(event.get('record'))

    def on_agent_request(self, request: dict):
        """An agent call, as the executor opens and closes it; the closed one replaces the open one, also when
        it comes after its run ended"""
        folder = self._agent_folder(request.get('id'))

        if folder is not None:
            self._safely(write_json, folder / f'{request["id"]}.json', request)
            self._note_captures(request)

    def on_agent_message(self, kind: str, message: dict):
        """A decision from the session, or the executor's reply to it"""
        folder = self._agent_folder(message.get('id'))

        if folder is not None:
            self._append(folder / f'{message["id"]}.messages.jsonl', {'kind': kind, 'time': self.clock(), **message})

    def close(self):
        self._safely(self._end_run, {'end': 'closed', 'text': 'the recorder stopped during the run'})
        self._events.close()
        self._finalize('failed' if self.failure else 'closed')

    # ========== Runs ==========

    def _start_run(self, event: dict):
        self._end_run({'end': 'halted', 'text': 'a new run started before this one ended'})

        self.runs += 1
        name   = event.get('name') or 'task'
        folder = self.path / 'runs' / f'{self.runs:03d}-{name}'
        (folder / 'steps').mkdir(parents=True)
        self.run = folder

        write_json(self.run / 'library.json', self.library())      # digests of the skills this run could use

        write_json(self.run / 'task.json', {
            'run_id':  event.get('run_id'),
            'name':    name,
            'blind':   bool(event.get('blind')),
            'started': event.get('time'),
            'task':    self.task,                 # the planner's last task; none for send_tree
            'places':  event.get('places'),       # the blackboard at the start: the places and their poses
        })
        self.task    = None
        self.scratch = event.get('scratch')
        self.bag.start(self.run / 'bag')

    def _end_run(self, event: dict):
        if self.run is None:
            return

        self._end_step({'kind': 'halted', 'text': 'the task ended during the step'})
        self._safely(self.bag.stop)
        self._copy_captures()
        self._move_scratch()
        self._safely(write_json, self.run / 'outcome.json', {**self._ending(event, event.get('end')), 'steps': event.get('steps')})
        self.run = None

    def _start_step(self, event: dict):
        self._end_step({'kind': 'halted', 'text': 'a new step started before this one ended'})

        step      = event.get('step') or 1
        tree      = event.get('tree_id') or 'tree'
        self.step = {'step': step, 'tree_id': tree, 'subgoal': event.get('subgoal'), 'started': event.get('time')}

        (self.run / 'steps' / f'{step:03d}-{tree}.xml').write_text(event.get('tree', ''))

    def _end_step(self, event: dict):
        if self.step is None:
            return

        step, self.step = self.step, None
        self._append(self.run / 'steps.jsonl', {**step, **self._ending(event, event.get('kind')),
                                                'status': event.get('status'), 'aborted': event.get('aborted')})

    def _ending(self, event: dict, end: str | None) -> dict:
        """The fields that a step's line and a task's outcome share"""
        return {'end': end, 'text': event.get('text'), 'finished': event.get('time', self.clock())}

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
            if source.is_file():
                self._safely(shutil.copy2, source, folder / source.name)

    def _move_scratch(self):
        """The agent's notes and checks of this run, moved in: a rename on one file system, so no bytes are copied in
        the trace callback, and a write after the run ended fails and does not change the bundle"""
        source, self.scratch = self.scratch, None

        if source and Path(source).is_dir():
            self._safely(shutil.move, source, self.run / 'scratch')

    def _agent_folder(self, call: str | None) -> Path | None:
        """The agent folder of the run the call opened in; None for a call outside any run"""
        if not call:
            return None

        if call not in self.calls:
            if self.run is None:
                return None

            self.calls[call] = self.run / 'agent'

        folder = self.calls[call]

        return folder if self._safely(folder.mkdir, exist_ok=True) else None

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
