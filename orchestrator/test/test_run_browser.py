import json

from orchestrator.mission_control import Commands, MissionApi, MissionState, RunShelf
from orchestrator.recording import Recorder, RecorderConfig
from orchestrator.run_browser import list_runs, run_detail


def event(kind, run_id, time, **fields):
    return {'source': 'executor', 'kind': kind, 'time': time, 'text': fields.pop('text', kind), 'run_id': run_id, **fields}


def _session(root):
    """Two runs as the executor reports them: one ended with a failed step, a refusal and a skill written; one open"""
    recorder = Recorder(RecorderConfig(root=str(root), min_free_gb=0.0),
                        {'git': [{'path': '/ws/src/nav', 'revision': 'abc', 'dirty': True}]},
                        library=lambda: {'say/contract.yaml': 'd1', 'walk/contract.yaml': 'd2', 'walk/code.py': 'd3'})
    recorder.on_task('{"task": "Walk to the call panel", "blind": false}')      # as the task topic carries it

    for item in [event('task_started', 'r1', 10.0, name='task'),
                 event('plan', 'r1', 11.0, version=1, subgoals=['walk there']),
                 event('step_gate', 'r1', 11.5, state='approved', skill='navigate_to'),
                 event('started', 'r1', 12.0, step=1, tree_id='navigate_to', subgoal='walk there', tree='<root/>'),
                 event('finished', 'r1', 132.0, status='failure'),
                 event('step_gate', 'r1', 140.0, state='refused', skill='cancel_navigation', subgoal='walk there',
                       reason='clear the map', by='mission_control'),
                 event('operator_note', 'r1', 145.0, state='queued', note='look at the door frames', step=1),
                 event('operator_note', 'r1', 146.0, state='given', notes=['look at the door frames']),
                 event('write_skill', 'r1', 150.0, text='wait_for_lio saved, untested'),
                 event('step_gate', 'r9', 151.0, state='refused', skill='say'),          # another run's
                 event('task_finished', 'r1', 160.0, end='gave_up', text='the map stays stale', steps=1),
                 event('task_started', 'r2', 200.0, name='discovery'),
                 event('started', 'r2', 210.0, step=1, tree_id='read_code', subgoal='read the stack', tree='<root/>')]:
        recorder.on_event(item)

    return recorder


def test_the_runs_are_listed_newest_first_with_their_outcome(tmp_path):
    old = tmp_path / '20200101T000000Z-aaaaaa' / 'runs' / '001-task'               # an older session
    old.mkdir(parents=True)
    (old / 'task.json').write_text(json.dumps({'run_id': 'r0', 'name': 'task'}))
    _session(tmp_path)

    runs = list_runs(tmp_path)

    assert [run['run_id'] for run in runs] == ['r2', 'r1', 'r0']
    assert runs[2]['end'] == 'lost'                                                    # its recorder stopped: never open
    assert runs[0]['end'] is None and runs[0]['name'] == 'discovery'                  # still open
    assert runs[1] | {'key': None} == {'key': None, 'run_id': 'r1', 'name': 'task', 'task': 'Walk to the call panel',
                                       'blind': False, 'started': 10.0, 'finished': 160.0, 'end': 'gave_up',
                                       'reason': 'the map stays stale', 'steps': 1, 'failed': 1}
    assert list_runs(tmp_path, limit=1) == runs[:1] and list_runs(tmp_path / 'nowhere') == []

    (old.parent / '1000-task').mkdir()                                               # run 1000 comes after run 999
    (old.parent / '999-task').mkdir()

    assert [run['key'].split('/')[1] for run in list_runs(tmp_path)][2:5] == ['1000-task', '999-task', '001-task']


def test_one_run_has_its_steps_refusals_what_it_left_and_its_code(tmp_path):
    recorder = _session(tmp_path)
    key = list_runs(tmp_path)[1]['key']

    run = run_detail(tmp_path, key)

    assert [step['status'] for step in run['steps']] == ['failure'] and run['plans'][0]['subgoals'] == ['walk there']
    assert run['refused'] == [{'skill': 'cancel_navigation', 'subgoal': 'walk there', 'reason': 'clear the map',
                               'by': 'mission_control', 'time': 140.0}]                  # not r9's
    assert [item['kind'] for item in run['left']] == ['write_skill']
    assert run['notes'] == [{'text': 'look at the door frames', 'step': 1, 'time': 145.0}]    # once: a nudge, for the count
    assert run['skills'] == 2 and run['git'][0]['dirty'] and run['bag'] is None
    assert run_detail(tmp_path, '../' + key) is None and run_detail(tmp_path, 'nothing') is None   # never a path

    recorder.on_event(event('step_gate', 'r1', 145.0, state='refused', skill='say'))   # the events file grew: read again

    assert len(run_detail(tmp_path, key)['refused']) == 2

    running = run_detail(tmp_path, list_runs(tmp_path)[0]['key'])                     # its step has no line yet

    assert [(step['tree_id'], step['status'], step['started']) for step in running['steps']] == [('read_code', None, 210.0)]

    recorder.on_event(event('finished', 'r2', 220.0, status='success'))               # between two steps: no open step
    between = run_detail(tmp_path, list_runs(tmp_path)[0]['key'])

    assert [(step['tree_id'], step['status']) for step in between['steps']] == [('read_code', 'success')]


def test_the_runs_tab_lists_and_opens_a_run(tmp_path):
    _session(tmp_path)
    api = MissionApi(MissionState(), Commands(None, None, None, None, None), runs=RunShelf(tmp_path))

    code, listing = api.get('/api/runs', {})
    key           = listing['runs'][1]['key']

    assert code == 200 and api.get('/api/run', {'key': key})[1]['run_id'] == 'r1'
    assert api.get('/api/run', {})[0] == 400 and api.get('/api/run', {'key': 'x/y'})[0] == 404


def test_the_task_sentence_is_read_from_the_request_or_as_it_is(tmp_path):
    recorder = Recorder(RecorderConfig(root=str(tmp_path), min_free_gb=0.0), {'git': []})
    recorder.on_task('Say hello')
    recorder.on_event(event('task_started', 'r3', 1.0, name='task'))

    assert list_runs(tmp_path)[0]['task'] == 'Say hello'


def test_the_reason_of_the_end_is_said_once(tmp_path):
    run = tmp_path / '20260101T000000Z-bbbbbb' / 'runs' / '001-task'
    run.mkdir(parents=True)
    (run / 'task.json').write_text(json.dumps({'run_id': 'r5'}))

    for outcome, reason in [({'end': 'gave_up', 'text': 'task r5: gave_up: the door is shut'}, 'the door is shut'),
                            ({'end': 'done', 'text': 'task r5: done'}, None),
                            ({'end': 'done', 'text': 'task r5: done', 'reason': 'arrived'}, 'arrived')]:
        (run / 'outcome.json').write_text(json.dumps(outcome))

        assert list_runs(tmp_path)[0]['reason'] == reason
