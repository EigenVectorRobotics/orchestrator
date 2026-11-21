import json
import threading
import http.client

from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import yaml

from skill_registry.contract import AGENT_MARK

from orchestrator.images import Camera, Outlines
from orchestrator.knowledge import KnowledgeStore, Sighting
from orchestrator.mission_control import (Commands, KnowledgeShelf, MissionApi, MissionState, SkillShelf, StaleSkills,
                                          finite, live_names, token_ok)


def _api(reply: dict | None = {'accepted': True, 'message': 'approved'}, listening: bool = True):
    state = MissionState(keep=3)
    sent  = SimpleNamespace(tasks=[], decisions=[], cancels=0, stops=[])

    def task(request):
        sent.tasks.append(request)
        return listening

    def decide(decision):
        sent.decisions.append(decision)
        return reply

    def cancel():
        sent.cancels += 1
        return listening

    def stop(request):
        sent.stops.append(request)
        return listening

    return MissionApi(state, Commands(task, decide, cancel, stop)), state, sent


def test_a_stop_names_the_run_and_the_step_on_the_page():
    api, _, sent = _api()

    assert api.post('/api/stop_step', {'run_id': 'r1', 'step': 3, 'reason': 'wrong way'})[0] == 200
    assert [api.post('/api/stop_step', body)[0] for body in ({'run_id': 'r1'}, {'step': 3}, {'run_id': 'r1', 'step': True},
                                                              {'run_id': 'r1', 'step': '3'})] == [400] * 4
    assert sent.stops == [{'run_id': 'r1', 'step': 3, 'reason': 'wrong way'}]
    assert _api(listening=False)[0].post('/api/stop_step', {'run_id': 'r1', 'step': 3})[0] == 504


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
    state.trace({'kind': 'step_gate', 'time': 21.5, 'state': 'refused', 'skill': 'give_up', 'end': 'gave_up', 'step': 2,
                 'reason': 'look at the door frames'})                            # a refused end is not a step
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


def test_a_late_event_of_a_closed_task_and_a_refusal_with_no_step_number_stay_out_of_the_steps():
    api, state, _ = _api()

    state.put('task', {'state': 'open', 'run_id': 'r1'})                           # the open message may come first
    state.trace({'kind': 'task_started', 'time': 10.0, 'run_id': 'r1'})
    state.trace({'kind': 'plan', 'run_id': 'r1', 'version': 1, 'subgoals': ['walk there']})
    state.trace({'kind': 'started', 'time': 11.0, 'run_id': 'r1', 'step': 1, 'tree_id': 'code_skill', 'subgoal': 'walk there'})
    state.trace({'kind': 'step_gate', 'time': 12.0, 'run_id': 'r1', 'state': 'refused', 'skill': 'code_skill'})
    # the desk's word that code changed after an approval: the approved step's answer is in the plan already
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
    api      = MissionApi(MissionState(), Commands(None, None, None, None), outlines=outlines)

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
    api = MissionApi(MissionState(), Commands(None, None, None, None),
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
    assert MissionApi(MissionState(), Commands(None, None, None, None)).get('/api/skills', {})[0] == 404   # no shelf, no tab

    gone = MissionApi(MissionState(), Commands(None, None, None, None),
                      skills=SkillShelf(tmp_path / 'nowhere', None, tmp_path / 'removed'))

    assert gone.get('/api/skills', {})[1]['skills'] == [] and 'no folder' in gone.get('/api/skills', {})[1]['problem']


def test_the_knowledge_tab_shows_the_store_and_the_operator_acts_on_it(tmp_path):
    store    = KnowledgeStore(tmp_path)
    claim, _ = store.note('The call panel does not light.', Sighting(by='agent:task', time=1.0, run='r1', value=False),
                          {'place': 'call_panel'}, 'lights_when_pressed')

    for index in range(10):
        store.confirm(claim.id, Sighting(by='operator' if index % 2 else 'check:door', time=2.0 + index, run=f'r{index + 2}'))

    store.stage_applied('done1', [claim.id], 'press_button', 3)
    store.stage('done1', claim.id, 'useful', 'it did not light, as said')
    api = MissionApi(MissionState(), Commands(None, None, None, None), knowledge=KnowledgeShelf(tmp_path))

    code, shown = api.get('/api/knowledge', {})

    assert code == 200 and shown['claims'][0]['ready'] and shown['claims'][0]['confirmed'] == 11
    assert shown['staged'][0]['run'] == 'done1' and shown['staged'][0]['grades'][0]['grade'] == 'useful'
    assert api.post('/api/knowledge/commit', {'run': 'done1'})[0] == 404           # the agent commits, not the page

    assert api.post('/api/knowledge/approve', {'id': claim.id, 'text': '  '})[0] == 400
    assert api.post('/api/knowledge/approve', {'id': 'C-9999'})[0] == 409
    assert api.post('/api/knowledge/approve', {'id': claim.id, 'text': 'The call panel never lights.'})[0] == 200

    fact = store.get(claim.id)
    log  = [json.loads(line) for line in store.log_path.read_text().splitlines()]

    assert fact.tier == 'fact' and fact.text == 'The call panel never lights.'
    assert log[-1]['action'] == 'approve' and log[-1]['by'] == 'operator' and log[-1]['via'] == 'mission_control'

    assert api.post('/api/knowledge/seen', {'id': claim.id})[0] == 200
    assert api.post('/api/knowledge/retire', {'id': claim.id})[0] == 200 and store.get(claim.id).tier == 'retired'
    assert api.post('/api/knowledge/retire', {'id': claim.id})[0] == 409            # retired already

    off = MissionApi(MissionState(), Commands(None, None, None, None), knowledge=KnowledgeShelf(tmp_path), token_needed=False)

    assert off.post('/api/knowledge/seen', {'id': claim.id})[0] == 403               # no token: no act as the operator
    assert off.get('/api/knowledge', {})[0] == 200                                   # reading needs none


def test_the_knowledge_tab_applies_expiry_before_it_shows_a_tier_and_says_a_broken_store(tmp_path):
    now      = [1000.0]
    store    = KnowledgeStore(tmp_path, clock=lambda: now[0])
    claim, _ = store.note('A person stands at the door.', Sighting(by='agent:task', time=1000.0, run='r1'), valid_for_s=60)
    shelf    = KnowledgeShelf(tmp_path, clock=lambda: now[0])

    now[0] = 2000.0                                                                 # it expired; nothing wrote since

    assert shelf.get()[1]['claims'][0]['tier'] == 'retired'

    (tmp_path / 'claims.yaml').write_text('- id: [')
    shelf._expired = -1e9

    code, said = shelf.get()

    assert code == 500 and 'cannot read the knowledge store' in said['message']


def test_the_skills_tab_says_which_skills_are_stale_and_live(tmp_path):
    library = tmp_path / 'skills'

    for name in ('say_it', 'walk'):
        (library / name).mkdir(parents=True)
        (library / name / 'contract.yaml').write_text(yaml.safe_dump(
            {'name': name, 'kind': 'action', 'device': 'nuc', 'timeout_s': 5,
             'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'}}))

    shelf = SkillShelf(library, None, tmp_path / 'removed', stale=lambda: ({'walk': 'the code moved under it'}, ''),
                       live=lambda: {'say_it'})
    items = {item['name']: item for item in shelf.get()[1]['skills']}

    assert (items['walk']['stale'], items['walk']['live']) == ('the code moved under it', False)
    assert (items['say_it']['stale'], items['say_it']['live']) == ('', True)

    unknown = SkillShelf(library, None, tmp_path).get()[1]                    # no clock, no registry_node: not known

    assert [(item['stale'], item['live']) for item in unknown['skills']] == [(None, None), (None, None)]


def test_the_live_list_is_names_or_nothing():
    assert live_names('["walk", "say_it"]') == {'walk', 'say_it'}
    assert live_names('[{"name": "walk", "maturity": "tested"}]') == {'walk'}             # a richer list later
    assert live_names('"walk"') is None and live_names('not json') is None                # never the letters of a name
    assert live_names('[{"maturity": "tested"}]') is None and live_names('[1]') is None and live_names('[[1]]') is None


def _clock_files(tmp_path, stamps_text: str):
    library, stamps = tmp_path / 'skills', tmp_path / 'stamps.yaml'
    (library / 'walk').mkdir(parents=True, exist_ok=True)
    (library / 'walk' / 'contract.yaml').write_text(yaml.safe_dump(
        {'name': 'walk', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5,
         'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'}}))
    (tmp_path / 'code_clock.yaml').write_text(yaml.safe_dump({'skills': str(library), 'stamps': str(stamps), 'repo_roots': []}))
    stamps.write_text(stamps_text)

    return library


def test_stale_skills_come_from_the_executor_s_stamps_and_are_kept_a_while(tmp_path):
    now   = [0.0]
    stale = StaleSkills(tmp_path, tmp_path / 'skills', every_s=30.0, clock=lambda: now[0])

    assert stale()[0] is None and 'no executor' in stale()[1]                  # no clock yet: not known, not "none"

    _clock_files(tmp_path, yaml.safe_dump({'walk': {'self': 'not the file', 'repos': {}}}))

    assert stale()[0] is None                                                  # said 0 s ago: kept

    now[0] = 31.0

    assert stale() == ({'walk': 'its own contract was edited after it was stamped'}, '')
    assert 'another library' in StaleSkills(tmp_path, tmp_path / 'other')()[1]


def test_a_bad_stamps_file_makes_every_skill_stale_with_the_reason_and_is_kept_a_while(tmp_path):
    library = _clock_files(tmp_path, '- walk\n')                               # a list, not stamps: nothing is proved
    now     = [0.0]
    stale   = StaleSkills(tmp_path, library, every_s=30.0, clock=lambda: now[0])

    assert 'is not a mapping of skill names to stamps' in stale()[0]['walk']

    (tmp_path / 'code_clock.yaml').unlink()                                    # not looked at again within every_s

    assert 'walk' in stale()[0] and 'no executor' not in stale()[1]


def test_the_state_is_strict_json_when_feedback_holds_inf():
    feedback = {'trace': [{'feedback': [{'distance_to_goal': float('inf'), 'error': float('nan'), 'ok': 1.5}]}]}

    text = json.dumps(finite(feedback), allow_nan=False)    # the browser reads only strict JSON (2026-11-20)

    assert json.loads(text)['trace'][0]['feedback'][0] == {'distance_to_goal': None, 'error': None, 'ok': 1.5}


def test_the_page_answers_only_under_a_local_name():
    """DNS rebinding: a page on another site reaches 127.0.0.1 under its own name, and could then read the state"""
    pytest.importorskip('rclpy')
    from orchestrator.mission_control_node import LOCAL, Handler       # a ROS node module: not needed by the other tests

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.api, server.files, server.token = MissionApi(MissionState(), Commands(None, None, None, None)), {'index.html': (b'page', 'text/html')}, 't'
    server.log, server.names = SimpleNamespace(warning=lambda text: None), {*LOCAL, '127.0.0.1'}
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def status(host, path):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        connection.request('GET', path, headers={'Host': host})
        return connection.getresponse().status

    try:
        assert status(f'localhost:{server.server_port}', '/api/state') == 200
        assert status(f'127.0.0.1:{server.server_port}', '/') == 200
        assert status(f'evil.example:{server.server_port}', '/api/state') == 403
    finally:
        server.shutdown()
        server.server_close()
