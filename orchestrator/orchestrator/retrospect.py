"""Retrospection from a shell: read the recorder's run bundles after the runs (docs/recorder.md, "Retrospection").

    retrospect list [--since 2026-11-19] [--limit 20]    the runs, newest first: outcome, steps, the task
    retrospect show RUN                                  one run: plans, each step with its calls, grades, what it left
    retrospect blame RUN                                 the evidence of a failure: from the step before the first
                                                         failed step to the end, the calls with their feedback and
                                                         the agent's words, in time order

RUN is a run_id (as the task gives it) or a key from list (<session>/<folder>). --root (default ~/orch_runs) is the
recorder's root, and --knowledge (default $EIGEN_VECTOR_WS/agent/knowledge) the folder of the step grades.

It only reads. The agent uses it to see what a run did before it grades a step, writes a claim, or writes a tree.
blame does not say a cause: the agent finds the cause from the evidence.
"""

import sys
import json
import argparse

from pathlib import Path
from datetime import datetime

from skill_registry import agent_knowledge_dir

from .recording import RecorderConfig
from .run_browser import list_runs, run_detail, step_failed
from .knowledge import KnowledgeError
from .step_grades import StepGrades


CUT      = 160                 # characters of inputs or feedback on one line
WORDS    = 480                 # characters of one item of the agent's words
SAMPLES  = 20                  # feedback states blame shows of one call: the first and the changes
TIME_KEYS = {'t', 'header', 'stamp'}   # feedback fields that only say when


def _when(stamp, form: str = '%Y-%m-%d %H:%M') -> str:
    return datetime.fromtimestamp(stamp).strftime(form) if stamp else '?'


def _span(start, end) -> str:
    return f'{end - start:.1f} s' if start and end else ''


def _short(value, cut: int = CUT) -> str:
    text = (value if isinstance(value, str) else json.dumps(value, default=str)).replace('\n', ' ')
    return text if len(text) <= cut else text[:cut - 3] + '...'


def run_lines(runs: list[dict]) -> list[str]:
    return [f'{_when(run["started"])}  {run["run_id"] or "?":8}  {run["end"] or "open":9}  '
            f'{run["steps"]} steps ({run["failed"]} failed)  {_short(run["task"] or "")}  [{run["key"]}]'
            for run in runs]


def _last_feedback(record: dict) -> list[str]:
    if record.get('status') == 'failure' and record.get('feedback'):
        return [f'      last feedback {_short(record["feedback"][-1])}']

    return []


def _call_lines(record: dict, feedback=_last_feedback) -> list[str]:
    """A call: its status, time, inputs, message and outputs; feedback gives the lines of its feedback"""
    lines = [f'    {record.get("skill")}  {record.get("status")}  {record.get("elapsed_s")} s  '
             f'inputs {_short(record.get("inputs", {}))}' + (f'  "{_short(record["message"])}"' if record.get('message') else '')]

    if record.get('outputs'):
        lines.append(f'      outputs {_short(record["outputs"])}')

    return lines + feedback(record)


def _head(detail: dict) -> list[str]:
    return [f'run {detail["run_id"]} [{detail["key"]}]', f'task: {detail["task"]}' + (' (blind)' if detail['blind'] else '')]


def _step_head(step: dict) -> str:
    return (f'step {step.get("step")}: {step.get("tree_id")}  {step.get("status") or step.get("end") or "running"}  '
            f'{_span(step.get("started"), step.get("finished"))}  subgoal: {step.get("subgoal")}')


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
    lines          = _head(detail) + [
                      f'started {_when(detail["started"])}, ended {detail["end"] or "not yet"} after {ended} steps'
                      + (f', {span}' if span else '') + (f': {detail["reason"]}' if detail['reason'] else '')]

    for plan in detail['plans']:
        lines.append(f'plan v{plan.get("version")}: ' + '; '.join(map(str, plan.get('subgoals') or [])))

    listed = set()

    for step in detail['steps']:
        listed.add(step.get('step'))
        lines += [_step_head(step)] + _step_lines(step.get('step'), detail['calls'], grades)

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


def _state(value):
    """What a feedback sample says, with no fractions and no time fields: a state, a phase, a flag, a text. A distance
    or a pose changes at each sample, and a state does not. The nested messages of a sample are read the same way"""
    if isinstance(value, dict):
        if set(value) == {'sec', 'nanosec'}:                    # a time or a duration
            return None

        kept = {key: _state(item) for key, item in value.items() if key not in TIME_KEYS}
        return {key: item for key, item in kept.items() if item not in (None, {})}

    if isinstance(value, list):
        return [_state(item) for item in value]

    return None if isinstance(value, float) else value


def _changes(record: dict) -> list[str]:
    """The samples where the state changes (_state), with the first, and then the last sample in full. A call keeps
    up to 1200 samples. Most of them only say that a distance went down"""
    feedback = record.get('feedback') or []

    if not feedback:
        return []

    states = [_state(sample) for sample in feedback]
    kept   = [f't {sample.get("t") if isinstance(sample, dict) else "?"}  {_short(states[index])}'
              for index, sample in enumerate(feedback) if index == 0 or states[index] != states[index - 1]]
    gone   = len(kept) - SAMPLES

    if gone > 0:
        kept = kept[:SAMPLES // 2] + [f'... {gone} more changes ...'] + kept[-SAMPLES // 2:]

    return ([f'      feedback: {len(feedback)} sample(s); the state at the start and at each change:']
            + [f'        {line}' for line in kept] + [f'      last sample: {_short(feedback[-1])}'])


def _first_failed(steps: list[dict], calls: dict) -> int | None:
    """The index in steps of the first step that did not succeed, or that has a failed call"""
    failed = {number for number, records in calls.items() if any(record.get('status') == 'failure' for record in records)}

    return next((index for index, step in enumerate(steps) if step_failed(step) or step.get('step') in failed), None)


def _cut_short(calls: dict, listed: set) -> list[dict]:
    """The steps that have calls but no step line: the recorder stopped in them. Each starts at its first call"""
    numbers = sorted(set(calls) - listed, key=lambda number: number if isinstance(number, int) else -1)

    return [{'step': number, 'tree_id': '(no step line: the recorder stopped in it?)', 'end': 'cut short',
             'started': min((record['time'] for record in calls[number] if record.get('time')), default=None)}
            for number in numbers]


def blame_lines(detail: dict) -> list[str]:
    head   = _head(detail) + [f'ended {detail["end"] or "not yet"}' + (f': {detail["reason"]}' if detail['reason'] else '')]
    steps  = detail['steps'] + _cut_short(detail['calls'], {step.get('step') for step in detail['steps']})
    first  = _first_failed(steps, detail['calls'])

    if first is None and detail['end'] in ('done', None):
        return head + ['no step failed: nothing to blame']

    before = (first if first is not None else len(steps)) - 1          # the step before: the decision that led there
    since  = (steps[before].get('started') if before >= 0 else None) or detail['started'] or 0
    lines  = head + ([f'first failed step: {steps[first].get("step")} ({steps[first].get("tree_id")})'] if first is not None
                     else ['no step failed, but the run did not end done'])
    items  = []

    for step in steps[max(before, 0):]:
        items.append((step.get('started'), [_step_head(step)]))
        items += [(record.get('time'), _call_lines(record, _changes)) for record in detail['calls'].get(step.get('step'), [])]

    words = [word for word in detail['words'] or [] if (word.get('time') or 0) >= since]

    for word in words:
        title = f'[{word["title"]}] ' if word.get('title') else ''
        items.append((word.get('time'), [f'{word.get("who")} {word.get("kind")}: {title}{_short(word.get("text") or "", WORDS)}']))

    lines.append('evidence, in time order (a call at its end; a line with no time comes last):')

    for when, text in sorted(items, key=lambda item: item[0] if item[0] is not None else float('inf')):
        lines += [f'{_when(when, "%H:%M:%S")}  {text[0]}'] + [f'          {line}' for line in text[1:]]

    if detail['words'] is None:
        lines.append('no words.jsonl: the recorder did not read the agent\'s words then')
    elif not words:
        lines.append(f'no words of the agent in this part ({len(detail["words"])} before it)')

    lines.append('frames: not recorded yet')
    lines.append(f'folder: {detail["folder"]}')
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
    blame    = commands.add_parser('blame')
    blame.add_argument('run', help='a run_id, or a key from list')
    arguments = parser.parse_args(argv)

    if arguments.command == 'list':
        since = arguments.since.timestamp() if arguments.since else None
        runs  = list_runs(arguments.root, None if since else arguments.limit)
        runs  = [run for run in runs if since is None or (run['started'] or 0) >= since][:arguments.limit]
        print('\n'.join(run_lines(runs)) or 'no runs')
        return 0

    detail = run_detail(arguments.root, arguments.run, full=arguments.command == 'blame')

    if detail is None:
        print(f'no run {arguments.run} under {arguments.root}', file=sys.stderr)
        return 1

    if arguments.command == 'blame':
        print('\n'.join(blame_lines(detail)))
        return 0

    print('\n'.join(show_lines(detail, (arguments.knowledge or agent_knowledge_dir()).expanduser())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
