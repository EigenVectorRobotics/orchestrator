from types import SimpleNamespace

from orchestrator.mission_control import Camera, Commands, MissionApi, MissionState


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
    assert sent.tasks == [{'task': 'Walk to the call panel', 'blind': True}]
    assert api.post('/api/task', {'task': '   '})[0] == 400
    assert api.post('/api/task', {'task': 'x' * 401})[0] == 400
    assert api.post('/api/nothing', {})[0] == 404
    assert api.get('/api/nothing', {})[0] == 404
    assert len(sent.tasks) == 1

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
