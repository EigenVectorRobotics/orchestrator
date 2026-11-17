import os
import sys
import time
import types
import asyncio
import argparse

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import claude_agent_sdk
except ImportError:                     # the model path is not under test here: a stand-in lets the bridge import
    sys.modules['claude_agent_sdk'] = types.SimpleNamespace(ClaudeAgentOptions=dict, ClaudeSDKClient=None, ResultMessage=None)

import agent_bridge


CONFIRM = {'type': 'call', 'id': 'c1', 'skill': 'operator_confirm', 'phase': 'confirm', 'images': [],
           'decisions': ['confirmed', 'not_confirmed'],
           'task': 'Phase: confirm.\nQuestion: Is the gantry removed?\nAllowed decisions: confirmed, not_confirmed.'}


async def _until(condition, timeout_s: float = 2.0):
    deadline = time.monotonic() + timeout_s

    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        await asyncio.sleep(0.01)


async def _bridge(tmp_path: Path, models: list) -> tuple:
    """A bridge without a link: what it sends lands in sent, and stdin is a pipe the test writes to"""
    bridge   = agent_bridge.Bridge(argparse.Namespace(local=tmp_path, model='m', effort='low', keep=False))
    sent     = []
    read, write = os.pipe()

    async def send(header: dict):
        sent.append(header)

    bridge.send     = send
    bridge._model   = lambda skill, decisions: models.append(skill)
    bridge.operator = agent_bridge.Operator(read)

    return bridge, sent, write


@pytest.mark.parametrize('typed, decision, reason', [
    (b'y gantry is off\n', 'confirmed', 'Operator: gantry is off'),
    (b'maybe\nN\n', 'not_confirmed', 'Operator: n'),
])
def test_a_confirm_asks_the_operator_and_never_the_model(tmp_path, capsys, typed, decision, reason):
    async def main():
        models = []
        bridge, sent, stdin = await _bridge(tmp_path, models)
        os.write(stdin, b'y typed before the question\n')
        await asyncio.sleep(0.05)

        await bridge._dispatch(dict(CONFIRM), b'', time.time())
        await _until(lambda: 'Is the gantry removed?' in capsys.readouterr().out)
        os.write(stdin, typed)
        await _until(lambda: any(message['type'] == 'decision' for message in sent))
        await bridge._dispatch({'type': 'reply', 'id': 'c1', 'accepted': True, 'message': 'ok'}, b'', time.time())
        await _until(lambda: 'c1' not in bridge.running)

        return models, sent

    models, sent = asyncio.run(main())

    assert models == []
    assert {key: value for key, value in sent[0].items() if key != 'received'} == \
        {'type': 'started', 'id': 'c1', 'model': 'operator', 'effort': None, 'prompt': 'Is the gantry removed?'}
    assert isinstance(sent[0]['received'], float)                       # the bridge's receive time, for the NUC record
    assert [(message['decision'], message['reason']) for message in sent if message['type'] == 'decision'] == [(decision, reason)]


def test_the_question_rings_the_bell(tmp_path, capsys):
    async def main():
        bridge, sent, stdin = await _bridge(tmp_path, [])
        await bridge._dispatch(dict(CONFIRM), b'', time.time())
        await asyncio.sleep(0.05)
        bridge.running['c1'].cancel()
        await _until(lambda: 'c1' not in bridge.running)

    asyncio.run(main())

    assert '\a' in capsys.readouterr().out


def test_a_cancelled_confirm_drops_the_question(tmp_path, capsys):
    async def main():
        bridge, sent, stdin = await _bridge(tmp_path, [])
        await bridge._dispatch(dict(CONFIRM), b'', time.time())
        await asyncio.sleep(0.05)
        await bridge._dispatch({'type': 'cancel', 'id': 'c1'}, b'', time.time())
        await _until(lambda: 'c1' not in bridge.running)
        os.write(stdin, b'y\n')
        await asyncio.sleep(0.05)

        return sent

    sent = asyncio.run(main())

    assert [message['type'] for message in sent] == ['started']
    assert 'c1: question dropped' in capsys.readouterr().out


def test_a_confirm_nobody_answers_is_dropped_at_the_timeout(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(agent_bridge, 'CONFIRM_S', 0.1)

    async def main():
        bridge, sent, stdin = await _bridge(tmp_path, [])
        await bridge._dispatch(dict(CONFIRM), b'', time.time())
        await _until(lambda: 'c1' not in bridge.running)

        return sent

    sent = asyncio.run(main())

    assert [message['type'] for message in sent] == ['started', 'failed']
    assert 'c1: question dropped' in capsys.readouterr().out
