import json
import subprocess

from orchestrator.recording.recorder import Recorder, RecorderConfig, find_repos, git_state, saved_files


class FakeBag:
    def __init__(self):
        self.calls = []

    def start(self, folder):
        self.calls.append(('start', folder))

    def stop(self):
        self.calls.append(('stop',))


def make_recorder(tmp_path, bag=None, min_free_gb=0.0):
    config = RecorderConfig(root=str(tmp_path / 'runs'), min_free_gb=min_free_gb, capture_dir=str(tmp_path / 'captures'))
    return Recorder(config, {'git': []}, bag)


def executor(kind, **fields):
    return {'source': 'executor', 'kind': kind, 'time': 1.0, 'text': fields.pop('text', kind), **fields}


def test_a_session_has_provenance_events_and_finalization(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder.on_event({'source': 'planner', 'kind': 'sent', 'text': 'tree sent'})
    recorder.close()

    assert json.loads((recorder.path / 'provenance.json').read_text())['schema_version'] == 1
    assert json.loads((recorder.path / 'events.jsonl').read_text())['kind'] == 'sent'
    assert json.loads((recorder.path / 'finalization.json').read_text())['status'] == 'closed'


def test_a_tree_run_gets_its_folder_tree_task_records_and_outcome(tmp_path):
    bag      = FakeBag()
    recorder = make_recorder(tmp_path, bag)

    recorder.on_task('Go to the elevator and call it')
    recorder.on_event(executor('started', run_id='r1', tree_id='call_elevator', tree='<root/>', blind=True))
    recorder.on_event(executor('record', record={'skill': 'navigate_to', 'status': 'success'}))
    recorder.on_event(executor('finished', status='success', aborted=''))

    run  = recorder.path / 'runs' / '001-call_elevator'
    task = json.loads((run / 'task.json').read_text())

    assert (run / 'tree.xml').read_text() == '<root/>'
    assert task['run_id'] == 'r1' and task['blind'] and task['task']['task'] == 'Go to the elevator and call it'
    assert json.loads((run / 'records.jsonl').read_text())['skill'] == 'navigate_to'
    assert json.loads((run / 'outcome.json').read_text())['status'] == 'success'
    assert bag.calls == [('start', run / 'bag'), ('stop',)]
    assert recorder.run is None


def test_the_task_goes_to_the_tree_after_preflight_too(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder.on_task('Take the freight elevator to target_floor_1.')
    recorder.on_event(executor('started', run_id='r1', tree_id='preflight', tree='<root/>'))
    recorder.on_event(executor('finished', status='success', aborted=''))
    recorder.on_event(executor('started', run_id='r2', tree_id='freight_elevator_go_to_place', tree='<root/>'))

    runs = recorder.path / 'runs'
    for folder in ('001-preflight', '002-freight_elevator_go_to_place'):
        assert json.loads((runs / folder / 'task.json').read_text())['task']['task'] == 'Take the freight elevator to target_floor_1.'

    recorder.on_event(executor('finished', status='success', aborted=''))
    recorder.on_event(executor('started', run_id='r3', tree_id='by_hand', tree='<root/>'))
    assert json.loads((runs / '003-by_hand' / 'task.json').read_text())['task'] is None


def test_a_new_tree_ends_the_open_run_as_halted(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder.on_event(executor('started', tree_id='a'))
    recorder.on_event(executor('halted', text='halted a for a new tree'))
    recorder.on_event(executor('started', tree_id='b'))

    assert json.loads((recorder.path / 'runs' / '001-a' / 'outcome.json').read_text())['end'] == 'halted'
    assert recorder.run == recorder.path / 'runs' / '002-b'


def test_agent_calls_are_kept_closed_over_open_with_their_messages(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder.on_event(executor('started', tree_id='t'))
    recorder.on_agent_request({'id': 'c1', 'state': 'open', 'skill': 'manipulation_recovery', 'phase': 'diagnose', 'opened': 10.0})
    recorder.on_agent_message('event', {'id': 'c1', 'kind': 'started', 'time': 11.0, 'model': 'm', 'effort': 'low', 'prompt': 'p',
                                        'system': 's', 'system_sha256': 'aa', 'prompts_sha256': 'bb'})
    recorder.on_agent_message('event', {'id': 'c1', 'kind': 'decision', 'time': 12.0, 'decision': {'decision': 'retry'},
                                        'accepted': True, 'cost_usd': 0.02, 'tokens_in': 500, 'tokens_out': 30})
    recorder.on_agent_message('reply', {'id': 'c1', 'accepted': True, 'message': 'retry'})
    recorder.on_agent_request({'id': 'c1', 'state': 'closed', 'skill': 'manipulation_recovery', 'opened': 10.0, 'closed': 13.0,
                               'outcome': 'answered', 'error': None})

    folder   = recorder.run / 'agent' / 'c1'
    messages = [json.loads(line) for line in (folder / 'messages.jsonl').read_text().splitlines()]
    summary  = json.loads((folder / 'summary.json').read_text())

    assert json.loads((folder / 'request.json').read_text())['state'] == 'closed'
    assert [message['kind'] for message in messages] == ['opened', 'started', 'decision', 'reply', 'closed']
    assert (messages[1]['system'], messages[1]['system_sha256'], messages[1]['prompts_sha256']) == ('s', 'aa', 'bb')
    assert (summary['system_sha256'], summary['prompts_sha256']) == ('aa', 'bb')
    assert (summary['model'], summary['prompt'], summary['state'], summary['decision']) == ('m', 'p', 'answered', {'decision': 'retry'})
    assert (summary['seconds'], summary['tokens_in'], summary['cost_usd']) == (3.0, 500, 0.02)
    assert not (recorder.path / 'agent_calls').exists()                                # one record per call, in its run


def test_agent_messages_after_the_run_ended_stay_with_their_run(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder.on_event(executor('started', tree_id='t'))
    run = recorder.run
    recorder.on_agent_request({'id': 'c1', 'state': 'open'})
    recorder.on_event(executor('halted', text='t cancelled'))
    recorder.on_agent_request({'id': 'c1', 'state': 'closed'})
    recorder.on_agent_message('reply', {'id': 'c1', 'accepted': True})
    recorder.on_agent_request({'id': 'c2', 'state': 'open'})          # outside any run: not kept

    assert json.loads((run / 'agent' / 'c1' / 'request.json').read_text())['state'] == 'closed'
    assert (run / 'agent' / 'c1' / 'messages.jsonl').exists()
    assert not (run / 'agent' / 'c2').exists()


def test_a_run_folder_that_cannot_be_made_fails_the_session_and_does_not_raise(tmp_path):
    recorder = make_recorder(tmp_path)

    (recorder.path / 'runs').write_text('a file where the runs folder goes')
    recorder.on_event(executor('started', tree_id='t'))

    assert recorder.run is None
    assert not recorder.ready()[0]


def test_press_frames_named_in_a_run_are_copied_in(tmp_path):
    recorder = make_recorder(tmp_path)
    frame    = tmp_path / 'captures' / 'press_1.000_press.jpg'
    frame.parent.mkdir()
    frame.write_bytes(b'jpeg')

    recorder.on_event(executor('started', tree_id='t'))
    run = recorder.run
    recorder.on_event(executor('record', record={'skill': 'press_button', 'outputs': {'frames': {'press': str(frame)}}}))
    recorder.on_event(executor('finished', status='failure'))

    assert (run / 'captures' / frame.name).read_bytes() == b'jpeg'
    assert not frame.exists()                       # the capture directory is a buffer: the run keeps the frame


def test_not_ready_below_min_free_gb(tmp_path):
    recorder      = make_recorder(tmp_path, min_free_gb=1e9)
    ready, reason = recorder.ready()

    assert not ready and 'below min_free_gb' in reason


def test_a_bag_stops_when_the_disk_fills_during_a_run(tmp_path):
    bag      = FakeBag()
    recorder = make_recorder(tmp_path, bag)

    recorder.on_event(executor('started', tree_id='t'))
    recorder.config.min_free_gb = 1e9
    recorder.check_bag_space()

    assert bag.calls[-1] == ('stop',)
    assert (recorder.run / 'bag_truncated').exists()


def test_a_failed_write_marks_the_session_failed_and_does_not_raise(tmp_path):
    recorder = make_recorder(tmp_path)

    recorder._events.close()                              # the next write fails
    recorder.on_event({'source': 'planner', 'kind': 'sent'})

    assert not recorder.ready()[0]
    assert json.loads((recorder.path / 'finalization.json').read_text())['status'] == 'failed'


def test_saved_files_finds_paths_under_the_prefix_at_any_depth():
    value = {'a': ['/c/x.jpg', {'b': '/c/y.jpg'}], 'n': 3, 'other': '/d/z.jpg'}

    assert saved_files(value, '/c/') == {'/c/x.jpg', '/c/y.jpg'}


def test_git_state_sees_local_edits(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*arguments):
        subprocess.run(['git', '-C', str(repo), *arguments], check=True, capture_output=True)

    git('init', '-q')
    (repo / 'a.txt').write_text('a')
    git('add', 'a.txt')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'a')

    assert not git_state(repo)['dirty']

    (repo / 'b.txt').write_text('b')
    first = git_state(repo)

    (repo / 'b.txt').write_text('changed')

    assert first['dirty'] and git_state(repo)['edits'] != first['edits']
    assert find_repos([str(tmp_path)]) == [repo]
