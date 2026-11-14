import json
import time
import socket
import threading

from pathlib import Path

from skill_registry import Registry

from orchestrator import agent_push
from orchestrator.agent import Agent, AgentConfig
from orchestrator.agent_push import PushAgentBackend, PushLink, read_message, send_message
from orchestrator.agent_sandbox import SandboxConfig
from orchestrator.behavior_tree import BehaviorTree, Context, Status
from orchestrator.skill_leaf import make_leaf_factory

from test_agent import CONTRACTS, JUDGE_TREE, PANEL, SKILLS_DIR, ROOT, FakeRunner


class FakeBridge:
    """The laptop bridge: accepts the executor's link and plays answer(header, blobs, conn) for each call"""

    def __init__(self, answer):
        self.answer   = answer
        self.received = []
        self.server   = socket.create_server(('127.0.0.1', 0))
        self.port     = self.server.getsockname()[1]
        self.conns    = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return

            self.conns.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn):
        conn.settimeout(5.0)

        try:
            while True:
                message = read_message(conn)

                if message is None or message[0]['type'] == 'ping':
                    continue

                self.received.append(message)

                if message[0]['type'] == 'call':
                    self.answer(message[0], message[1], conn)
        except (OSError, ValueError):
            return

    def wait_for(self, kind: str, count: int = 1, timeout_s: float = 5.0) -> list:
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            found = [message for message in self.received if message[0]['type'] == kind]

            if len(found) >= count:
                return found

            time.sleep(0.01)

        raise AssertionError(f'no {kind} from the executor')

    def close(self):
        self.server.close()

        for conn in self.conns:
            conn.close()


def _tree(xml_text: str, runner, agent) -> BehaviorTree:
    return BehaviorTree(xml_text, make_leaf_factory(Registry.from_directory(CONTRACTS), runner, agent), Context(blackboard={'call_panel': dict(PANEL)}))


def _agent(tmp_path: Path, backend, press: dict) -> Agent:
    config = AgentConfig(skills_dir=str(SKILLS_DIR), read_roots=[str(ROOT)], sandbox=SandboxConfig(scratch_dir=str(tmp_path / 'scratch')))

    return Agent(backend, config, press_evidence=lambda: press)


def _press(tmp_path: Path) -> dict:
    frames = {}

    for name in ('baseline', 'press', 'press_crop'):
        frames[name] = str(tmp_path / f'press_1.000_{name}.jpg')
        Path(frames[name]).write_bytes(f'jpeg {name}'.encode())

    return {'left_arm': False, 'goal': [0.4, -0.1, 1.0], 'capture': {'max_depth_m': -0.003, 'residual_m': [-0.003, 0.0, 0.0]}, 'frames': frames}


def _run(tree: BehaviorTree, max_ticks: int = 2000) -> Status:
    for _ in range(max_ticks):
        status = tree.tick()

        if status != Status.RUNNING:
            return status

        time.sleep(0.005)

    return Status.RUNNING


def _session(tmp_path: Path) -> Path:
    session = tmp_path / 'orch_runs' / '20261114T000000Z-abcdef'
    session.mkdir(parents=True)
    (session / 'events.jsonl').write_text('')

    return session


def test_a_pushed_call_carries_the_frames_and_a_refused_decision_goes_back(tmp_path):
    def answer(header, blobs, conn):
        send_message(conn, {'type': 'started', 'id': header['id'], 'model': 'm', 'effort': 'low', 'prompt': 'p'})
        send_message(conn, {'type': 'decision', 'id': header['id'], 'decision': 'continue', 'reason': 'wrong phase'})
        send_message(conn, {'type': 'decision', 'id': header['id'], 'decision': 'pressed', 'reason': 'ring lit',
                            'cost_usd': 0.01, 'tokens_in': 900, 'tokens_out': 40})

    session  = _session(tmp_path)
    bridge   = FakeBridge(answer)
    requests = []
    backend  = PushAgentBackend(PushLink('127.0.0.1', bridge.port), requests.append, records=tmp_path / 'orch_runs')
    tree     = _tree(JUDGE_TREE, FakeRunner([(True, 'Controller reached target', [])]), _agent(tmp_path, backend, _press(tmp_path)))

    try:
        assert _run(tree) == Status.SUCCESS
        header, blobs = bridge.wait_for('call')[0]
        replies       = [message[0] for message in bridge.wait_for('reply', 2)]
    finally:
        tree.halt()
        bridge.close()
        backend.link.close()

    assert header['skill'] == 'press_judge' and header['phase'] == 'judge' and header['decisions'] == ['pressed', 'not_pressed']
    assert [image['name'] for image in header['images']] == ['baseline', 'press', 'press_crop']
    assert blobs == [b'jpeg baseline', b'jpeg press', b'jpeg press_crop']
    assert [reply['accepted'] for reply in replies] == [False, True]
    assert 'must be one of' in replies[0]['message']
    assert [request['state'] for request in requests] == ['open', 'closed']

    record = json.loads((session / 'agent_calls' / f'{header["id"]}.json').read_text())
    assert record['state'] == 'answered' and record['decision']['decision'] == 'pressed'
    assert (record['model'], record['prompt'], record['tokens_in']) == ('m', 'p', 900)


def test_a_halted_call_tells_the_bridge_to_drop_it(tmp_path):
    session = _session(tmp_path)
    bridge  = FakeBridge(lambda header, blobs, conn: None)
    backend = PushAgentBackend(PushLink('127.0.0.1', bridge.port), records=tmp_path / 'orch_runs')
    tree    = _tree(JUDGE_TREE, FakeRunner([(True, 'Controller reached target', [])]), _agent(tmp_path, backend, _press(tmp_path)))

    try:
        for _ in range(1000):
            tree.tick()

            if [message for message in bridge.received if message[0]['type'] == 'call']:
                break

            time.sleep(0.005)

        tree.halt()
        cancel = bridge.wait_for('cancel')[0][0]
    finally:
        tree.halt()
        bridge.close()
        backend.link.close()

    assert cancel['id'] == bridge.received[0][0]['id']
    assert json.loads((session / 'agent_calls' / f'{cancel["id"]}.json').read_text())['state'] == 'failed'


def test_the_open_call_goes_again_when_the_bridge_comes_back(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_push, 'RETRY_S', 0.1)
    calls = []

    def first(header, blobs, conn):
        calls.append(header['id'])
        conn.shutdown(socket.SHUT_RDWR)          # the laptop restarts mid-call

    bridge  = FakeBridge(first)
    again   = None
    backend = PushAgentBackend(PushLink('127.0.0.1', bridge.port), records=tmp_path / 'orch_runs')
    tree    = _tree(JUDGE_TREE, FakeRunner([(True, 'Controller reached target', [])]), _agent(tmp_path, backend, _press(tmp_path)))

    try:
        for _ in range(1000):
            tree.tick()

            if calls:
                break

            time.sleep(0.005)

        again = FakeBridge(lambda header, blobs, conn: send_message(conn, {'type': 'decision', 'id': header['id'], 'reason': 'r',
                                                                         'decision': 'not_pressed' if header['phase'] == 'judge' else 'ask_help'}))
        backend.link.port = again.port          # the bridge comes back; a new port spares the test a rebind
        bridge.close()
        status = _run(tree)
        resent = again.wait_for('call')[0][0]
    finally:
        tree.halt()                             # the agent's worker thread ends, so pytest can exit
        bridge.close()
        backend.link.close()

        if again is not None:
            again.close()

    assert resent['id'] == calls[0]
    assert status == Status.FAILURE                     # not_pressed, then the recovery asks for help


def test_a_bridge_failure_fails_the_call(tmp_path):
    bridge  = FakeBridge(lambda header, blobs, conn: send_message(conn, {'type': 'failed', 'id': header['id'], 'error': 'no structured output'}))
    backend = PushAgentBackend(PushLink('127.0.0.1', bridge.port), records=tmp_path / 'orch_runs')
    tree    = _tree(JUDGE_TREE, FakeRunner([(True, 'Controller reached target', [])]), _agent(tmp_path, backend, _press(tmp_path)))

    try:
        assert _run(tree) == Status.FAILURE
    finally:
        tree.halt()
        bridge.close()
        backend.link.close()


def test_other_skills_go_to_the_fallback_for_a_person():
    class Fallback:
        def run(self, *arguments, skill: str = ''):
            self.skill = skill

    fallback = Fallback()
    backend  = PushAgentBackend(PushLink('127.0.0.1', 9), fallback=fallback)

    backend.run('', 'Phase: confirm.', [], lambda name, arguments: None, 5, lambda: False, lambda *a, **k: None, skill='operator_confirm')
    backend.link.close()

    assert fallback.skill == 'operator_confirm'
