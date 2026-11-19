import json

from orchestrator.recording import Recorder, RecorderConfig


class FakeBag:
    def __init__(self):
        self.calls = []

    def start(self, folder):
        self.calls.append(('start', folder))

    def stop(self):
        self.calls.append(('stop',))


def make_recorder(tmp_path, bag=None, min_free_gb=0.0):
    config = RecorderConfig(root=str(tmp_path / 'runs'), min_free_gb=min_free_gb)
    return Recorder(config, {'git': []}, bag)


def executor(kind, **fields):
    return {'source': 'executor', 'kind': kind, 'time': 1.0, 'text': fields.pop('text', kind), **fields}


def start_task(recorder, name='t', **fields):
    """A task of one step, as the executor reports it"""
    recorder.on_event(executor('task_started', name=name, **fields))
    recorder.on_event(executor('started', tree_id=name, step=1, tree='<root/>'))


def end_task(recorder, kind='finished', end='success', **fields):
    recorder.on_event(executor(kind, **fields))
    recorder.on_event(executor('task_finished', end=end, steps=1, **fields))


def test_a_session_has_provenance_events_and_finalization(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder.on_event({'source': 'mission_control', 'kind': 'sent', 'text': 'task sent'})
    recorder.close()

    assert 'schema_version' in json.loads((recorder.path / 'provenance.json').read_text())
    assert json.loads((recorder.path / 'events.jsonl').read_text())['kind'] == 'sent'
    assert json.loads((recorder.path / 'finalization.json').read_text())['status'] == 'closed'


def test_a_task_gets_its_folder_steps_task_records_and_outcome(tmp_path):
    bag      = FakeBag()
    recorder = make_recorder(tmp_path, bag)

    recorder.on_task('Go to the elevator and call it')
    start_task(recorder, 'call_elevator', run_id='r1', blind=True)
    recorder.on_event(executor('record', record={'skill': 'navigate_to', 'status': 'success'}))
    end_task(recorder, status='success')

    run  = recorder.path / 'runs' / '001-call_elevator'
    task = json.loads((run / 'task.json').read_text())
    step = json.loads((run / 'steps.jsonl').read_text())

    assert (run / 'steps' / '001-call_elevator.xml').read_text() == '<root/>'
    assert step['step'] == 1 and step['end'] == 'finished' and step['status'] == 'success'
    assert task['run_id'] == 'r1' and task['blind'] and task['task']['task'] == 'Go to the elevator and call it'
    assert json.loads((run / 'records.jsonl').read_text())['skill'] == 'navigate_to'
    assert json.loads((run / 'outcome.json').read_text())['end'] == 'success'
    assert bag.calls == [('start', run / 'bag'), ('stop',)]
    assert recorder.run is None


def test_a_new_task_ends_the_open_run_and_its_step_as_halted(tmp_path):
    recorder = make_recorder(tmp_path)

    start_task(recorder, 'a')
    start_task(recorder, 'b')

    run = recorder.path / 'runs' / '001-a'

    assert json.loads((run / 'outcome.json').read_text())['end'] == 'halted'
    assert json.loads((run / 'steps.jsonl').read_text())['end'] == 'halted'
    assert recorder.run == recorder.path / 'runs' / '002-b'


def test_steps_of_one_task_stay_in_one_run(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder.on_event(executor('task_started', name='task'))
    recorder.on_event(executor('started', tree_id='navigate_to', step=1))
    recorder.on_event(executor('finished', status='success'))
    recorder.on_event(executor('started', tree_id='press_button', step=2))
    recorder.on_event(executor('finished', status='failure'))
    recorder.on_event(executor('task_finished', end='gave_up', steps=2))

    run   = recorder.path / 'runs' / '001-task'
    steps = [json.loads(line) for line in (run / 'steps.jsonl').read_text().splitlines()]

    assert [step['tree_id'] for step in steps] == ['navigate_to', 'press_button']
    assert sorted(path.name for path in (run / 'steps').iterdir()) == ['001-navigate_to.xml', '002-press_button.xml']
    outcome = json.loads((run / 'outcome.json').read_text())

    assert (outcome['end'], outcome['steps']) == ('gave_up', 2)


def test_a_run_folder_that_cannot_be_made_fails_the_session_and_does_not_raise(tmp_path):
    recorder = make_recorder(tmp_path)

    (recorder.path / 'runs').write_text('a file where the runs folder goes')
    start_task(recorder)

    assert recorder.run is None
    assert not recorder.ready()[0]


def test_the_agent_scratch_folder_of_a_run_is_moved_in(tmp_path):
    recorder = make_recorder(tmp_path)
    scratch  = tmp_path / 'scratch' / 'r1'
    scratch.mkdir(parents=True)
    (scratch / 'plan.md').write_text('walk closer')

    start_task(recorder, scratch=str(scratch))
    run = recorder.run
    end_task(recorder, status='success')

    assert (run / 'scratch' / 'plan.md').read_text() == 'walk closer'
    assert not scratch.exists()


def test_not_ready_below_min_free_gb(tmp_path):
    recorder = make_recorder(tmp_path, min_free_gb=1e9)
    ready, _ = recorder.ready()

    assert not ready


def test_a_bag_stops_when_the_disk_fills_during_a_run(tmp_path):
    bag      = FakeBag()
    recorder = make_recorder(tmp_path, bag)

    start_task(recorder)
    recorder.config.min_free_gb = 1e9
    recorder.check_bag_space()

    assert bag.calls[-1] == ('stop',)
    assert (recorder.run / 'bag_truncated').exists()


def test_a_failed_write_marks_the_session_failed_and_does_not_raise(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder._events.close()                              # the next write fails
    recorder.on_event({'source': 'mission_control', 'kind': 'sent'})

    assert not recorder.ready()[0]
    assert json.loads((recorder.path / 'finalization.json').read_text())['status'] == 'failed'
