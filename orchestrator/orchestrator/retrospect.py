"""Retrospection from a shell: read the recorder's run bundles after the runs (docs/recorder.md, "Retrospection").

    retrospect list [--since 2026-11-19] [--limit 20]    the runs, newest first: outcome, steps, the task
    retrospect show RUN                                  one run: plans, each step with its calls, grades, what it left

RUN is a run_id (as the task gives it) or a key from list (<session>/<folder>). --root (default ~/orch_runs) is the
recorder's root, and --knowledge (default $EIGEN_VECTOR_WS/agent/knowledge) the folder of the step grades.

It only reads. The agent uses it to see what a run did before it grades a step, writes a claim, or writes a tree.
"""

import sys
import json
import argparse

from pathlib import Path
from datetime import datetime

from skill_registry import agent_knowledge_dir

from .recording import RecorderConfig
from .run_browser import list_runs, run_detail
from .knowledge import KnowledgeError
from .step_grades import StepGrades


CUT = 160                      # characters of inputs or feedback on one line


def _when(stamp) -> str:
    return datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M') if stamp else '?'


def _span(start, end) -> str:
    return f'{end - start:.1f} s' if start and end else ''


def _short(value) -> str:
    text = (value if isinstance(value, str) else json.dumps(value, default=str)).replace('\n', ' ')
    return text if len(text) <= CUT else text[:CUT - 3] + '...'


def run_lines(runs: list[dict]) -> list[str]:
    return [f'{_when(run["started"])}  {run["run_id"] or "?":8}  {run["end"] or "open":9}  '
            f'{run["steps"]} steps ({run["failed"]} failed)  {_short(run["task"] or "")}  [{run["key"]}]'
            for run in runs]


def _call_lines(record: dict) -> list[str]:
    lines = [f'    {record.get("skill")}  {record.get("status")}  {record.get("elapsed_s")} s  '
             f'inputs {_short(record.get("inputs", {}))}' + (f'  "{_short(record["message"])}"' if record.get('message') else '')]

    if record.get('outputs'):
        lines.append(f'      outputs {_short(record["outputs"])}')

    if record.get('status') == 'failure' and record.get('feedback'):
        lines.append(f'      last feedback {_short(record["feedback"][-1])}')

    return lines


def _grades(knowledge: Path, run_id: str | None) -> tuple[dict, str]:
    """The grades of the run's steps, and what to say when they cannot be read (a staged file edited by hand)"""
    try:
        return (StepGrades(knowledge).grades(run_id) if run_id else {}), ''
    except (KnowledgeError, OSError, ValueError) as error:
        return {}, f'grades not read from {knowledge}: {error}'


def _step_lines(number, calls: dict, grades: dict) -> list[str]:
    lines = [line for record in calls.get(number, []) for line in _call_lines(record)]
    return lines + ([f'    grade: {grades[number]}'] if number in grades else [])


def show_lines(detail: dict, knowledge: Path) -> list[str]:
    run_id         = detail['run_id']
    grades, failed = _grades(knowledge, run_id)
    span           = _span(detail['started'], detail['finished'])
    ended          = sum(1 for step in detail['steps'] if step.get('finished'))
    lines          = [f'run {run_id} [{detail["key"]}]',
                      f'task: {detail["task"]}' + (' (blind)' if detail['blind'] else ''),
                      f'started {_when(detail["started"])}, ended {detail["end"] or "not yet"} after {ended} steps'
                      + (f', {span}' if span else '') + (f': {detail["reason"]}' if detail['reason'] else '')]

    for plan in detail['plans']:
        lines.append(f'plan v{plan.get("version")}: ' + '; '.join(map(str, plan.get('subgoals') or [])))

    listed = set()

    for step in detail['steps']:
        number = step.get('step')
        listed.add(number)
        lines.append(f'step {number}: {step.get("tree_id")}  {step.get("status") or step.get("end") or "running"}  '
                     f'{_span(step.get("started"), step.get("finished"))}  subgoal: {step.get("subgoal")}')
        lines += _step_lines(number, detail['calls'], grades)

    for number in sorted((set(detail['calls']) | set(grades)) - listed, key=str):     # a run cut short: no step line
        lines += [f'step {number}: no step line (the recorder stopped in it?)'] + _step_lines(number, detail['calls'], grades)

    lines += [failed] if failed else []

    lines += [f'refused: {item["skill"]} ({item["subgoal"]}): {item["reason"]}' for item in detail['refused']]
    lines += [f'left: {item["kind"]}: {item["text"]}' for item in detail['left']]

    if detail['scratch']:
        lines.append(f'scratch: {", ".join(detail["scratch"])}')

    lines.append(f'grades from: {knowledge}')
    lines.append(f'folder: {detail["folder"]}' + (f'  (bag {detail["bag"] / 1e6:.1f} MB)' if detail['bag'] else ''))
    return lines


def main(argv=None) -> int:
    parser   = argparse.ArgumentParser(prog='retrospect', description='read the run bundles after the runs')
    parser.add_argument('--root', type=Path, default=Path(RecorderConfig().root))
    parser.add_argument('--knowledge', type=Path, default=None)
    commands = parser.add_subparsers(dest='command', required=True)
    listing  = commands.add_parser('list')
    listing.add_argument('--since', type=datetime.fromisoformat, help='a date or time, e.g. 2026-11-19 or 2026-11-19T14:00')
    listing.add_argument('--limit', type=int, default=20)
    show     = commands.add_parser('show')
    show.add_argument('run', help='a run_id, or a key from list')
    arguments = parser.parse_args(argv)

    if arguments.command == 'list':
        since = arguments.since.timestamp() if arguments.since else None
        runs  = list_runs(arguments.root, None if since else arguments.limit)
        runs  = [run for run in runs if since is None or (run['started'] or 0) >= since][:arguments.limit]
        print('\n'.join(run_lines(runs)) or 'no runs')
        return 0

    detail = run_detail(arguments.root, arguments.run)

    if detail is None:
        print(f'no run {arguments.run} under {arguments.root}', file=sys.stderr)
        return 1

    print('\n'.join(show_lines(detail, (arguments.knowledge or agent_knowledge_dir()).expanduser())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
