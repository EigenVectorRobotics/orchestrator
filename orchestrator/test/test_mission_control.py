from types import SimpleNamespace

import yaml

from skill_registry.contract import AGENT_MARK

from orchestrator.images import Camera, Outlines
from orchestrator.mission_control import Commands, MissionApi, MissionState, SkillShelf, token_ok


def _api(reply: dict | None = {'accepted': True, 'message': 'approved'}, listening: bool = True):
    state = MissionState(keep=3)
    sent  = SimpleNamespace(tasks=[], decisions=[], cancels=0)

    def task(request):
        sent.tasks.append(request)
        return listening

    def decide(decision):
        sent.decisions.append(decision)
        return reply

    def cancel():
        sent.cancels += 1
        return listening

    return MissionApi(state, Commands(task, decide, cancel)), state, sent


def test_the_state_carries_the_plan_and_only_the_new_trace_events():
    api, state, _ = _api()

    state.put('status', {'state': 'running'})
    state.trace({'kind': 'plan', 'text': 'plan v1', 'subgoals': ['walk there', 'press the button'], 'version': 1})
    state.trace({'kind': 'record', 'text': 'navigate_to success'})

    code, first = api.get('/api/state', {})

    assert code == 200 and first['status'] == {'state': 'running'}
    assert first['plan'] == {'version': 1, 'subgoals': ['walk there', 'press the button']}
    assert [event['kind'] for event in first['trace']] == ['plan', 'record']

    state.trace({'kind': 'record', 'text': 'press_button success'})
    _, second = api.get('/api/state', {'after': first['seq']})

    assert [event['text'] for event in second['trace']] == ['press_button success']

    state.put('task', {'state': 'open', 'run_id': 'r2'})        # a new task forgets the plan of the last one

    assert api.get('/api/state', {})[1]['plan'] == {}


def test_the_open_task_is_cut_down_to_what_the_page_shows():
    api, state, _ = _api()

    state.put('task', {'state': 'open', 'run_id': 'r1', 'task': 'Press the down button', 'blind': False,
                       'known': 'a long knowledge section', 'places': {'call_panel': {}}, 'scratch': '/tmp/r1'})

    assert api.get('/api/state', {})[1]['task'] == {'state': 'open', 'run_id': 'r1', 'task': 'Press the down button', 'blind': False}


def test_a_click_sends_one_decision_and_relays_what_the_gate_answers():
    api, _, sent = _api()

    assert api.post('/api/step', {'id': 'abc123', 'approve': True, 'reason': ''}) == (200, {'ok': True, 'message': 'approved'})
    assert sent.decisions == [{'id': 'abc123', 'approve': True, 'reason': '', 'by': 'mission_control'}]

    refused, _, sent = _api(reply={'accepted': False, 'message': 'this step was decided already'})
    code, payload    = refused.post('/api/step', {'id': 'abc123', 'approve': True})

    assert code == 409 and payload == {'ok': False, 'message': 'this step was decided already'}


def test_a_decision_without_a_step_or_without_approve_never_reaches_the_executor():
    api, _, sent = _api()

    assert api.post('/api/step', {'id': 'abc123'})[0] == 400                      # no approve: nothing is approved
    assert api.post('/api/step', {'id': 'abc123', 'approve': 'yes'})[0] == 400
    assert api.post('/api/step', {'approve': True})[0] == 400                     # no step named
    assert sent.decisions == []


def test_the_executor_may_not_answer_a_decision():
    api, _, sent = _api(reply=None)

    code, payload = api.post('/api/step', {'id': 'abc123', 'approve': True})

    assert code == 504 and 'did not take the decision' in payload['message'] and len(sent.decisions) == 1


def test_a_task_needs_a_sentence():
    api, _, sent = _api()

    assert api.post('/api/task', {'task': '  Walk to the\n  call panel ', 'blind': True})[0] == 200
    assert api.post('/api/task', {'task': 'Learn the stack', 'discovery': True})[0] == 200
    assert sent.tasks == [{'task': 'Walk to the call panel', 'blind': True, 'discovery': False},
                          {'task': 'Learn the stack', 'blind': False, 'discovery': True}]
    assert api.post('/api/task', {'task': '   '})[0] == 400
    assert api.post('/api/task', {'task': 'x' * 401})[0] == 400
    assert api.post('/api/nothing', {})[0] == 404
    assert api.get('/api/nothing', {})[0] == 404
    assert len(sent.tasks) == 2

    quiet, _, _ = _api(listening=False)             # the executor is not there: the page is told, not told "sent"

    assert quiet.post('/api/task', {'task': 'Press the down button'})[0] == 504
    assert quiet.post('/api/cancel', {})[0] == 504


def test_the_camera_encodes_a_frame_once_and_only_when_it_is_asked():
    frame  = SimpleNamespace(height=4, width=4, step=12, encoding='rgb8', data=bytes(48))
    camera = Camera('/camera/camera/color/image_raw')

    assert camera.state() == {'have': False, 'topic': '/camera/camera/color/image_raw'} and camera.frame() is None

    camera.put(frame)
    body, media_type = camera.frame()

    assert media_type in ('image/jpeg', 'image/png') and body
    assert camera.frame()[0] is body                        # the same frame is not encoded again
    assert camera.state()['width'] == 4

    camera.put(SimpleNamespace(height=4, width=4, step=12, encoding='rgb8', data=bytes(range(48))))

    assert camera.frame()[0] is not body


def test_a_command_needs_the_node_token():
    assert token_ok('s3cret', 's3cret')
    assert not token_ok('s3cret', 'guess') and not token_ok('s3cret', '')
    assert not token_ok('', '') and not token_ok('', 'anything')   # no token set is not an open door
    assert token_ok('off', '') and token_ok('off', 'anything')     # off is the one way to take the check away
    assert not token_ok('s3cret', 'café')                           # a header byte over 0x7F is refused, not a crash


def test_the_steps_of_the_open_task_come_from_the_trace():
    api, state, _ = _api()

    state.trace({'kind': 'task_started', 'time': 10.0})
    state.trace({'kind': 'started', 'time': 11.0, 'step': 1, 'tree_id': 'navigate_to', 'subgoal': 'walk there'})
    state.trace({'kind': 'finished', 'time': 20.0, 'status': 'failure'})
    state.trace({'kind': 'step_gate', 'time': 21.0, 'state': 'refused', 'skill': 'cancel_navigation', 'subgoal': 'walk there',
                 'step': 2, 'reason': 'clear the map'})
    state.trace({'kind': 'step_gate', 'time': 22.0, 'state': 'approved', 'skill': 'clear_terrain_map', 'step': 2})
    state.trace({'kind': 'started', 'time': 22.0, 'step': 2, 'tree_id': 'clear_terrain_map', 'subgoal': 'clear the map'})

    snapshot = api.get('/api/state', {})[1]

    assert snapshot['began'] == 10.0
    assert [(step['skill'], step['status']) for step in snapshot['steps']] == [
        ('navigate_to', 'failure'), ('cancel_navigation', 'refused'), ('clear_terrain_map', 'running')]
    assert snapshot['steps'][0]['end'] == 20.0 and snapshot['steps'][1]['reason'] == 'clear the map'

    state.trace({'kind': 'halted', 'time': 23.0})
    state.trace({'kind': 'finished', 'time': 24.0, 'status': 'success'})          # no step runs: nothing to end

    assert [step['status'] for step in api.get('/api/state', {})[1]['steps']] == ['failure', 'refused', 'halted']

    state.trace({'kind': 'task_started', 'time': 30.0, 'run_id': 'r2'})            # a new task starts with no steps

    assert api.get('/api/state', {})[1]['steps'] == []


def test_a_late_event_of_a_closed_task_and_a_refusal_inside_a_code_skill_stay_out_of_the_steps():
    api, state, _ = _api()

    state.put('task', {'state': 'open', 'run_id': 'r1'})                           # the open message may come first
    state.trace({'kind': 'task_started', 'time': 10.0, 'run_id': 'r1'})
    state.trace({'kind': 'plan', 'run_id': 'r1', 'version': 1, 'subgoals': ['walk there']})
    state.trace({'kind': 'started', 'time': 11.0, 'run_id': 'r1', 'step': 1, 'tree_id': 'code_skill', 'subgoal': 'walk there'})
    state.trace({'kind': 'step_gate', 'time': 12.0, 'run_id': 'r1', 'state': 'refused', 'skill': 'navigate_to',
                 'step': 'inside the code'})
    state.trace({'kind': 'finished', 'time': 13.0, 'run_id': 'r1', 'status': 'failure'})

    snapshot = api.get('/api/state', {})[1]

    assert snapshot['began'] == 10.0 and snapshot['plan']['subgoals'] == ['walk there']
    assert [(step['skill'], step['status']) for step in snapshot['steps']] == [('code_skill', 'failure')]

    state.trace({'kind': 'task_started', 'time': 20.0, 'run_id': 'r2'})
    state.trace({'kind': 'step_gate', 'time': 21.0, 'run_id': 'r1', 'state': 'refused', 'skill': 'say', 'step': 2,
                 'reason': 'the task ended'})                                     # r1 closed; its gate answers late
    state.put('task', {'state': 'open', 'run_id': 'r2'})

    snapshot = api.get('/api/state', {})[1]

    assert snapshot['steps'] == [] and snapshot['began'] == 20.0

    state.put('task', {'state': 'open', 'run_id': 'r3'})                           # its task_started is not here yet

    assert api.get('/api/state', {})[1]['began'] is None


def _marker(points, colors=(), color=(0.0, 1.0, 0.0)):
    point = lambda x, y: SimpleNamespace(x=x, y=y)
    rgb   = lambda r, g, b: SimpleNamespace(r=r, g=g, b=b)

    return SimpleNamespace(points=[point(*p) for p in points], outline_colors=[rgb(*c) for c in colors], outline_color=rgb(*color))


def test_the_mask_outlines_are_segments_with_a_colour_each_and_only_while_a_page_asks():
    outlines = Outlines(keep_s=60.0)
    api      = MissionApi(MissionState(), Commands(None, None, None), outlines=outlines)

    assert not outlines.wanted() and api.get('/api/state', {})[1]['outlines'] == {}

    outlines.put(_marker([(1, 2), (3, 4), (5, 6), (7, 8)], colors=[(1, 0, 0), (1, 0, 0), (0, 0, 1), (0, 0, 1)]))
    shown = api.get('/api/state', {'outlines': '1'})[1]['outlines']

    assert outlines.wanted()
    assert shown['segments'] == [[1, 2, 3, 4], [5, 6, 7, 8]] and shown['colors'] == ['#ff0000', '#0000ff']

    outlines.put(_marker([(0, 0), (9, 9)]))                                        # no colour per point: the one colour

    assert outlines.state()['colors'] == ['#00ff00']

    outlines.clear()

    assert outlines.state() == {}


def test_old_outlines_and_an_old_ask_count_for_nothing():
    outlines = Outlines(keep_s=0.0, ask_s=0.0)

    outlines.put(_marker([(0, 0), (9, 9)]))
    outlines.ask()

    assert outlines.state() == {} and not outlines.wanted()


def test_the_skills_tab_lists_the_library_and_keeps_or_removes_an_agent_skill(tmp_path):
    library = tmp_path / 'skills'
    (library / 'say_it').mkdir(parents=True)
    (library / 'say_it' / 'contract.yaml').write_text(AGENT_MARK + yaml.safe_dump(
        {'name': 'say_it', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5,
         'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'}}))
    api = MissionApi(MissionState(), Commands(None, None, None),
                     skills=SkillShelf(library, tmp_path / 'stamps.yaml', tmp_path / 'removed'))

    code, shelf = api.get('/api/skills', {})

    assert code == 200 and [item['name'] for item in shelf['skills']] == ['say_it'] and 'rt/' in shelf['deny']
    version = shelf['skills'][0]['version']

    assert api.post('/api/skill/keep', {})[0] == 400
    assert api.post('/api/skill/keep', {'name': 'say_it'})[0] == 400                 # no version read: nothing changes
    assert api.post('/api/skill/remove', {'name': 'nothing', 'version': version})[0] == 409
    assert api.post('/api/skill/keep', {'name': 'say_it', 'version': version})[0] == 200
    assert api.post('/api/skill/remove', {'name': 'say_it', 'version': version}) == (409, {'ok': False, 'message':
        'say_it was written or kept by a person: remove it in git'})
    assert MissionApi(MissionState(), Commands(None, None, None)).get('/api/skills', {})[0] == 404   # no shelf, no tab

    gone = MissionApi(MissionState(), Commands(None, None, None),
                      skills=SkillShelf(tmp_path / 'nowhere', None, tmp_path / 'removed'))

    assert gone.get('/api/skills', {})[1]['skills'] == [] and 'no folder' in gone.get('/api/skills', {})[1]['problem']
