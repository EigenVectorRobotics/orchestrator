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
