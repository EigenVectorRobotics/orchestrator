import json

from orchestrator.retrospect import main, show_lines
from orchestrator.run_browser import run_detail
from orchestrator.step_grades import StepGrades

from test_run_browser import _session, event


def test_show_gives_each_step_with_its_calls_and_grade_found_by_run_id(tmp_path, capsys):
    recorder = _session(tmp_path / 'runs')
    recorder.on_event(event('plan', 'r2', 205.0, version=1, subgoals=['read', 2]))         # a session's odd item
    recorder.on_event(event('record', 'r2', 211.0, step=1, record={
        'skill': 'navigate_to', 'status': 'failure', 'elapsed_s': 10.2, 'inputs': {'goal': 'panel'},
        'message': 'blocked\nfor 10 s', 'feedback': [{'state': 'moving'}, {'state': 'blocked'}],
        'checked_call': {'skill': 'walk', 'feedback': [1, 2, 3]}}))
    recorder.on_event(event('record', 'r1', 120.0, step=1, record={'skill': 'other_run', 'status': 'success'}))
    recorder.on_event(event('record', 'r2', 212.0, step=2, record={'skill': 'other_step', 'status': 'success',
                                                                        'elapsed_s': 0.1, 'message': 'x' * 200}))
    grades = StepGrades(tmp_path / 'knowledge')
    grades.stage_step('r2', 1, 'navigate_to', 'v1', 'finished')
    grades.stage('r2', 1, 'blocked', 'the cabin door closed')
    grades.commit('r2')
    grades.stage_step('r9', 1, 'say', 'v1', 'finished')                       # another run's grade
    grades.stage('r9', 1, 'worked', 'it spoke')
    grades.commit('r9')
    grades.stage_step('r3', 1, 'say', 'v1', 'finished')
    assert grades.grades('r3') == {1: 'not graded'}                            # staged, with no grade yet

    detail = run_detail(tmp_path / 'runs', 'r2')
    lines  = show_lines(detail, tmp_path / 'knowledge')

    assert detail['calls'][1][0]['checked_call']['feedback'] == [3]          # the page gets only the last item
    assert lines[2].endswith('ended not yet after 0 steps')                    # step 1 still runs
    assert lines[3] == 'plan v1: read; 2'
    assert lines[4].startswith('step 1: read_code  running')
    assert lines[5:8] == ['    navigate_to  failure  10.2 s  inputs {"goal": "panel"}  "blocked for 10 s"',
                          '      last feedback {"state": "blocked"}',
                          '    grade: blocked (committed): the cabin door closed']
    assert lines[8:10] == ['step 2: no step line (the recorder stopped in it?)',     # its step line is missing
                           f'    other_step  success  0.1 s  inputs {{}}  "{"x" * 157}..."']
    assert 'other_run' not in '\n'.join(lines)

    (tmp_path / 'knowledge' / 'staged_steps' / 'r2.json').write_text('{cut')               # edited by hand
    assert any(line.startswith('grades not read') for line in show_lines(detail, tmp_path / 'knowledge'))

    assert main(['--root', str(tmp_path / 'runs'), 'show', 'nothing']) == 1
    assert main(['--root', str(tmp_path / 'runs'), 'list', '--since', '1970-01-01T00:03+00:00']) == 0
    assert [line.split()[2] for line in capsys.readouterr().out.splitlines()] == ['r2']     # r1 started at 10 s


def test_blame_gives_the_evidence_from_the_step_before_the_failure_in_time_order(tmp_path):
    from orchestrator.retrospect import SAMPLES, blame_lines

    recorder = _session(tmp_path / 'runs')
    states   = ['moving', 'blocked'] * SAMPLES
    recorder.on_event(event('record', 'r1', 132.0, step=1, record={
        'skill': 'navigate_to', 'status': 'failure', 'message': 'blocked',
        'feedback': [{'t': 0.1, 'state': 'moving', 'dist': 3.0}, {'t': 0.2, 'state': 'moving', 'dist': 2.0}]
                    + [{'t': 1.0 + index, 'state': state, 'dist': 1.0} for index, state in enumerate(states)]}))
    run = next((tmp_path / 'runs').glob('*/runs/001-task'))
    (run / 'words.jsonl').write_text('\n'.join(json.dumps(word) for word in [
        {'kind': 'thought', 'time': 5.0, 'who': 'agent', 'title': '', 'text': 'before the run'},
        {'kind': 'said', 'time': 140.0, 'who': 'agent', 'title': '', 'text': 'the door\nis closed'},
        {'kind': 'thought', 'time': 11.0, 'who': 'agent', 'title': 'Plan', 'text': 'plan the walk'}]) + '\n')

    lines = blame_lines(run_detail(tmp_path / 'runs', 'r1', full=True))
    assert run_detail(tmp_path / 'runs', 'r1')['calls'][1][0]['time'] == 132.0       # the time of the call's end
    text  = '\n'.join(lines)

    assert lines[3] == 'first failed step: 1 (navigate_to)'
    assert 'before the run' not in text and 'plan the walk' in text              # step 1 failed: the words since the run start
    assert text.index('step 1: navigate_to') < text.index('agent said: the door is closed')
    assert f'feedback: {2 + len(states)} sample(s); the state at the start and at each change:' in text
    assert '"dist": 2.0' not in text                                            # only a number changed
    assert f'... {len(states) - SAMPLES} more changes ...' in text           # the first, then each change of state
    assert blame_lines(run_detail(tmp_path / 'runs', 'r2', full=True))[-1] == 'no step failed: nothing to blame'
    (run / 'words.jsonl').unlink()
    assert "no words.jsonl: the recorder did not read the agent's words then" in blame_lines(run_detail(tmp_path / 'runs', 'r1', full=True))


def test_blame_starts_at_the_decision_before_a_failed_step_and_keeps_the_last_sample():
    from orchestrator.retrospect import blame_lines

    step   = lambda number, start, status: {'step': number, 'tree_id': f's{number}', 'started': start, 'finished': start + 1,
                                            'status': status, 'end': 'finished', 'subgoal': 'go'}
    detail = {'run_id': 'r', 'key': 'k', 'task': 't', 'end': 'gave_up', 'reason': None, 'started': 1.0, 'folder': 'f', 'blind': False,
              'steps': [step(1, 10.0, 'success'), step(2, 20.0, 'failure')],          # no record says step 2 failed
              'calls': {1: [{'skill': 'walk', 'status': 'success',
                             'feedback': [{'state': 'moving', 'd': 3.0}, {'state': 'moving', 'd': 0.5}]}]},
              'words': [{'time': 5.0, 'who': 'agent', 'kind': 'thought', 'text': 'too early'},
                        {'time': 15.0, 'who': 'agent', 'kind': 'thought', 'text': 'try the stairs'}]}
    text   = '\n'.join(blame_lines(detail))

    assert 'first failed step: 2 (s2)' in text and 'too early' not in text
    assert text.index('step 1: s1') < text.index('try the stairs') < text.index('step 2: s2')
    assert '"d": 0.5' in text                                                     # the last sample, though no state changed

    detail['steps'][1]['status'] = 'success'
    assert 'no step failed, but the run did not end done' in blame_lines(detail)


def test_blame_counts_a_halted_step_and_the_calls_of_a_step_with_no_step_line():
    from orchestrator.retrospect import blame_lines

    detail = {'run_id': 'r', 'key': 'k', 'task': 't', 'end': None, 'reason': None, 'started': 1.0, 'folder': 'f',
              'blind': False, 'words': [],
              'steps': [{'step': 1, 'tree_id': 'walk', 'started': 10.0, 'end': 'halted', 'status': None},
                        {'step': 2, 'tree_id': 'say', 'started': 20.0, 'end': None, 'status': None}],     # still runs
              'calls': {3: [{'skill': 'press', 'status': 'success'}]}}
    text   = '\n'.join(blame_lines(detail))

    assert 'first failed step: 1 (walk)' in text
    assert text.index('step 2: say  running') < text.index('step 3: (no step line') < text.index('    press  success')
    assert 'no words of the agent in this part (0 before it)' in text


def test_blame_sees_a_state_in_nested_feedback_and_puts_each_call_at_its_end():
    from orchestrator.retrospect import blame_lines

    sample = lambda t, phase: {'t': t, 'header': {'seq': int(t), 'stamp': {'sec': int(t), 'nanosec': 7}},
                                'pose': {'x': t / 3, 'y': 1.5}, 'path': [t, 2.0],
                                'elapsed': {'sec': int(t), 'nanosec': 9}, 'phase': phase}
    detail = {'run_id': 'r', 'key': 'k', 'task': 't', 'end': 'gave_up', 'reason': None, 'started': 1.0, 'folder': 'f',
              'blind': False,
              'steps': [{'step': 1, 'tree_id': 'walk', 'started': 10.0, 'end': 'finished', 'status': 'failure'}],
              'calls': {1: [{'skill': 'walk', 'status': 'failure', 'time': 30.0,
                             'feedback': [sample(1.0, 1), sample(2.0, 1), sample(3.0, 3)]},
                            {'skill': 'look', 'status': 'success', 'time': 12.0}]},
              'words': [{'time': 20.0, 'who': 'agent', 'kind': 'said', 'text': 'still walking'}]}
    lines  = blame_lines(detail)
    text   = '\n'.join(lines)

    assert [line.strip() for line in lines if line.strip().startswith('t ')] == ['t 1.0  {"path": [null, null], "phase": 1}',
                                                                               't 3.0  {"path": [null, null], "phase": 3}']
    assert text.index('look  success') < text.index('still walking') < text.index('    walk  failure')
