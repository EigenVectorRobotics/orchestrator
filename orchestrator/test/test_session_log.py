import json
import os
import time

from datetime import datetime, timezone

from orchestrator import session_log
from orchestrator.mission_control import MissionState, SessionFollower
from orchestrator.session_log import SessionLog


T0 = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc).timestamp()


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(T0 + seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def _meta(cwd, **more) -> dict:
    return {'timestamp': _stamp(0), 'type': 'session_meta', 'payload': {'id': 's1', 'cwd': str(cwd), **more}}


def _thought(seconds, text, id=None) -> dict:
    return {'timestamp': _stamp(seconds), 'type': 'response_item',
            'payload': {'type': 'reasoning', 'id': id, 'summary': [{'type': 'summary_text', 'text': text}] if text else [],
                        'encrypted_content': 'x'}}


def _said(seconds, text) -> dict:
    return {'timestamp': _stamp(seconds), 'type': 'response_item',
            'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}}


def _log(root, name, records, end='\n', days_before=0):
    day  = datetime.fromtimestamp(T0 - days_before * 86400)
    path = root / f'{day:%Y/%m/%d}' / f'rollout-{name}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(record) for record in records) + end)
    os.utime(path, (T0 + 60, T0 + 60))
    return path


def test_the_agent_s_thoughts_and_words_come_once_and_nothing_else(tmp_path):
    agent, other, root = tmp_path / 'agent', tmp_path / 'elsewhere', tmp_path / 'sessions'
    agent.mkdir(); other.mkdir()
    _log(root, 'main', [
        _meta(agent),
        _thought(-5, '**Before the mission**\n\nnot shown'),                     # older than the mission
        _thought(10, '**Planning the ride**\n\nI will find the button first.', id='rs_1'),
        {'timestamp': _stamp(10), 'type': 'event_msg', 'payload': {'type': 'agent_reasoning', 'text': 'Planning the ride'}},
        {'timestamp': _stamp(11), 'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command',
                                                                       'arguments': '{"cmd": "cat secrets"}'}},
        {'timestamp': _stamp(11), 'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'a secret'}},
        _thought(12, ''),                                                         # Codex runs without summaries
        {'timestamp': _stamp(13), 'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': 5}},
        _said(20, 'The button is at the lift.'),
        _said(30, 'The button is at the lift.'),                                  # said again: a new item
    ])
    _log(root, 'worker', [_meta(agent, agent_nickname='scout'),
                          _thought(10, '**Planning the ride**\n\nI will find the button first.', id='rs_1'),   # the parent's copies
                          _said(14, 'The button is at the lift.'),
                          _thought(15, 'I read the map.', id='rs_2')], days_before=3)   # a resumed session stays in its first day
    _log(root, 'another', [_meta(other), _thought(10, 'another project', id='rs_9')])

    log   = SessionLog([agent], root)
    items = log.new(T0)

    assert [(item['kind'], item['who'], item['title'], item['text']) for item in items] == [
        ('thought', 'agent', 'Planning the ride', 'I will find the button first.'),
        ('thought', 'scout', '', 'I read the map.'),
        ('said', 'agent', '', 'The button is at the lift.'),
        ('said', 'agent', '', 'The button is at the lift.')]
    assert log.new(T0) == []
    assert log.status() == {'sessions': 2, 'empty': 1, 'quiet': 'codex'}
    assert log.new(T0 + 100) == [] and log.status() == {'sessions': 0, 'empty': 0, 'quiet': None}   # the next mission counts again


def test_a_line_codex_has_not_ended_is_read_when_it_ends_and_a_huge_line_is_skipped(tmp_path, monkeypatch):
    root, agent = tmp_path / 'sessions', tmp_path / 'agent'
    agent.mkdir()
    path = _log(root, 'main', [], end=json.dumps(_meta(agent))[:30])                # even the first line
    log  = SessionLog([agent], root)

    assert log.new(T0) == []

    _log(root, 'main', [_meta(agent), _said(5, 'first')], end='\n' + json.dumps(_said(6, 'second'))[:40])
    assert [item['text'] for item in log.new(T0)] == ['first']

    path.write_text('\n'.join(json.dumps(record) for record in [_meta(agent), _said(5, 'first'), _said(6, 'second')]) + '\n')
    assert [item['text'] for item in log.new(T0)] == ['second']

    monkeypatch.setattr(session_log, 'MAX_READ', 300)
    image = {'timestamp': _stamp(7), 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'x' * 700}}
    path.write_text('\n'.join(json.dumps(record) for record in [_meta(agent), _said(5, 'first'), _said(6, 'second'),
                                                                 image, _said(8, 'third')]) + '\n')
    assert [item['text'] for _ in range(4) for item in log.new(T0)] == ['third']


def test_the_feed_gets_the_agent_s_words_while_its_mission_is_open_and_just_after(tmp_path):
    agent, root = tmp_path / 'agent', tmp_path / 'sessions'
    agent.mkdir()
    _log(root, 'main', [_meta(agent), _said(5, 'on my way')])
    state, now = MissionState(), [0.0]
    follow     = SessionFollower(state, SessionLog([agent], root), clock=lambda: now[0])

    assert follow() == 0

    state.trace({'kind': 'task_started', 'run_id': 'r1', 'time': T0})
    state.put('task', {'state': 'open', 'run_id': 'r1'})
    assert follow() == 1

    snapshot = state.snapshot()
    assert [(word['kind'], word['run_id'], word['text']) for word in snapshot['words']] == [('said', 'r1', 'on my way')]
    assert snapshot['steps'] == []

    state.put('task', {'state': 'closed', 'run_id': 'r1'})
    _log(root, 'main', [_meta(agent), _said(5, 'on my way'), _said(9, 'done')])
    assert follow() == 1                                     # the last words, read just after the close

    now[0] += session_log.AFTER_END + 1
    _log(root, 'main', [_meta(agent), _said(5, 'on my way'), _said(9, 'done'), _said(12, 'too late')])
    assert follow() == 0


def test_a_new_run_gets_its_own_tail_after_its_end():
    now = [0.0]

    class Log:
        def new(self, since):
            return [{'since': since}]

    words = session_log.RunWords(Log(), clock=lambda: now[0])

    words('a', 1.0, False)
    now[0] += session_log.AFTER_END + 1

    assert words('a', 1.0, False) == []
    assert words('b', 2.0, False) == [{'since': 2.0}]


def _claude(seconds, *parts, type='assistant', **more) -> dict:
    return {'type': type, 'timestamp': _stamp(seconds), 'cwd': '/x', **more,
            'message': {'role': type, 'content': [{'type': kind, kind if kind != 'tool_use' else 'input': text}
                                                  for kind, text in parts]}}


def _claude_log(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(record) for record in records) + '\n')
    os.utime(path, (T0 + 60, T0 + 60))


def test_claude_code_thoughts_and_words_come_from_the_folder_of_the_agent_s_logs(tmp_path):
    real, root = tmp_path / 'ws' / 'agent_dir', tmp_path / 'claude'
    real.mkdir(parents=True)
    (tmp_path / 'agent').symlink_to(real)                    # the session starts in the link: Claude names the logs after it
    logs = root / session_log.claude_folder(tmp_path / 'agent')
    _claude_log(logs / 's1.jsonl', [
        _claude(-5, ('text', 'before the mission')),
        _claude(10, ('thinking', 'I will find the button first.'), ('text', 'Going to the lift.')),
        _claude(11, ('tool_use', {'command': 'cat secrets'})),
        {**_claude(12, ('text', 'Going to the lift.')), 'type': 'api-request-blob'},     # a copy of an earlier turn
        _claude(13, ('thinking', '')),                           # no showThinkingSummaries
    ])
    _claude_log(logs / 's1' / 'subagents' / 'agent-a1.jsonl', [_claude(14, ('text', 'Found it.'))])
    (logs / 's1' / 'subagents' / 'agent-a1.meta.json').write_text('{"agentType": "Explore"}')
    _claude_log(root / session_log.claude_folder(tmp_path / 'elsewhere') / 's2.jsonl', [_claude(15, ('text', 'not ours'))])

    log   = SessionLog([tmp_path / 'agent'], root=tmp_path / 'codex', claude_root=root)
    items = log.new(T0)

    assert [(item['kind'], item['who'], item['text']) for item in items] == [
        ('thought', 'agent', 'I will find the button first.'), ('said', 'agent', 'Going to the lift.'),
        ('said', 'Explore', 'Found it.')]
    assert log.status() == {'sessions': 2, 'empty': 1, 'quiet': 'claude'}
    assert log.new(T0) == []                                     # each item once

    _claude_log(logs / 's3.jsonl', [_claude(20, ('text', 'A new session.'))])   # the folder changes: listed again
    os.utime(logs, (T0 + 70, T0 + 70))
    assert [item['text'] for item in log.new(T0)] == ['A new session.']

    os.utime(logs / 's1.jsonl', (T0 - 60, T0 - 60))                           # the session waits for its worker
    _claude_log(logs / 's1' / 'subagents' / 'agent-a1.jsonl', [_claude(14, ('text', 'Found it.')), _claude(30, ('text', 'Still here.'))])
    assert [item['text'] for item in log.new(T0)] == ['Still here.']

    tick = time.time()                                                         # a new file in the same tick as the last listing
    os.utime(logs, (tick, tick)); log.new(T0)
    _claude_log(logs / 's4.jsonl', [_claude(40, ('text', 'Same tick.'))])
    os.utime(logs, (tick, tick))
    assert [item['text'] for item in log.new(T0)] == ['Same tick.']
