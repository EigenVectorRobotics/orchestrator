"""The run bundles of the recorder, to read: Mission Control's Runs tab.

A recorder session is a folder in the root (~/orch_runs/<utc>-<id>/) with provenance.json, events.jsonl and
finalization.json, and one folder per task in runs/ (recording.py). This module only reads them. A run is named by its
key, "<session>/<folder>", and a key from a page is looked up in the listing: it is never a path.

Limit: what a run left is read from its own session's events. A grade that the next task open commits in a later
session is in that session's events, not here.
"""

import json

from pathlib import Path

from .recording import read_lines


MAX_RUNS = 200                      # the newest runs the list shows
LEFT     = ('write_skill', 'knowledge', 'stale_skill')   # trace events that say what a run left behind


def _json(path: Path) -> dict:
    """A JSON file, or {} when it is missing or cut short (a run that is still open, a crash)"""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}

    return value if isinstance(value, dict) else {}




def _sentence(task) -> str | None:
    """The task sentence in task.json: the recorder keeps what came on the task topic, {task, time}, and that is the
    JSON request ({"task": sentence, "blind": ...}) or a plain sentence"""
    text = task.get('task') if isinstance(task, dict) else task

    try:
        request = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        request = None

    return request.get('task') if isinstance(request, dict) else text


def _number(run: Path) -> int:
    """The run's number in its session (recording.py names it f'{n:03d}-{name}'): run 1000 comes after run 999"""
    head = run.name.split('-', 1)[0]

    return int(head) if head.isdigit() else -1


def _folders(root: Path) -> list[Path]:
    """The run folders, newest session first, and in a session the newest run first"""
    sessions = sorted((path for path in Path(root).expanduser().glob('*/runs') if path.is_dir()), reverse=True)

    return [run for session in sessions for run in sorted(session.iterdir(), key=_number, reverse=True) if run.is_dir()]


def _live(run: Path, newest: Path | None) -> bool:
    """A run with no outcome is still open only in the newest session, while its recorder has not closed it"""
    session = run.parent.parent

    return session == newest and _json(session / 'finalization.json').get('status', 'open') == 'open'


def _key(run: Path) -> str:
    return f'{run.parent.parent.name}/{run.name}'


def _reason(outcome: dict, run_id) -> str | None:
    """Why the run ended: the reason the recorder keeps, or the trace text without its 'task <id>: <end>' head"""
    if outcome.get('reason') is not None:
        return outcome['reason'] or None

    text, head = outcome.get('text') or '', f'task {run_id}: {outcome.get("end")}'

    return text.removeprefix(head).removeprefix(': ') or None if text.startswith(head) else text or None


def _summary(run: Path, newest: Path | None) -> dict:
    task, outcome = _json(run / 'task.json'), _json(run / 'outcome.json')
    steps         = read_lines(run / 'steps.jsonl')
    end           = outcome.get('end') or (None if _live(run, newest) else 'lost')   # 'lost': the recorder stopped

    return {'key': _key(run), 'run_id': task.get('run_id'), 'name': task.get('name') or run.name.split('-', 1)[-1],
            'task': _sentence(task.get('task')), 'blind': bool(task.get('blind')), 'started': task.get('started'),
            'finished': outcome.get('finished'), 'end': end, 'reason': _reason(outcome, task.get('run_id')),
            'steps': len(steps), 'failed': sum(1 for step in steps if step.get('status') != 'success')}


def _newest(folders: list[Path]) -> Path | None:
    return folders[0].parent.parent if folders else None


def list_runs(root, limit: int = MAX_RUNS) -> list[dict]:
    """The newest runs, each as a summary. end is None while a run is open; 'lost' when its recorder stopped
    before it ended (a crash, a power cut), so it will never end"""
    folders = _folders(root)

    return [_summary(run, _newest(folders)) for run in folders[:limit]]


def _size(path: Path) -> int:
    try:
        return sum(file.stat().st_size for file in path.rglob('*') if file.is_file())
    except OSError:
        return 0


def run_detail(root, key: str) -> dict | None:
    """One run: its summary, steps, plans, the refusals and what it left (from its session's events), its bag and
    scratch folder, and the code it ran on (the session's provenance). None when no listed run has this key"""
    folders = _folders(root)
    run     = next((folder for folder in folders if _key(folder) == key), None)

    if run is None:
        return None

    session = run.parent.parent
    detail  = _summary(run, _newest(folders))
    run_id  = detail['run_id']
    events  = [event for event in read_lines(session / 'events.jsonl') if run_id and event.get('run_id') == run_id]
    scratch = run / 'scratch'
    steps   = read_lines(run / 'steps.jsonl')

    if detail['end'] is None:                       # the step that runs now: its line comes only when it ends
        ended   = {step.get('step') for step in steps}
        started = [event for event in events if event.get('kind') == 'started' and event.get('step') not in ended]
        steps   = steps + [{'step': event.get('step'), 'tree_id': event.get('tree_id'), 'subgoal': event.get('subgoal'),
                            'started': event.get('time'), 'end': None, 'finished': None, 'status': None}
                           for event in started[-1:]]

    return {**detail,
            'folder':   str(run),
            'steps':    steps,
            'plans':    read_lines(run / 'plans.jsonl'),
            'refused':  [{'skill': event.get('skill'), 'subgoal': event.get('subgoal'), 'reason': event.get('reason'),
                          'by': event.get('by'), 'time': event.get('time')}
                         for event in events if event.get('kind') == 'step_gate' and event.get('state') == 'refused'],
            'left':     [{'kind': event.get('kind'), 'text': event.get('text'), 'level': event.get('level'),
                          'time': event.get('time')} for event in events if event.get('kind') in LEFT],
            'bag':      _size(run / 'bag') if (run / 'bag').is_dir() else None,
            'scratch':  sorted(str(path.relative_to(scratch)) for path in scratch.rglob('*') if path.is_file())
                        if scratch.is_dir() else [],
            'skills':   sum(1 for name in _json(run / 'library.json') if name.endswith('/contract.yaml')),
            'git':      _json(session / 'provenance.json').get('git', [])}
