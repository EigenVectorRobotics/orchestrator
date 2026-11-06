import json
import math
import time

from pathlib import Path

import pytest

from skill_registry import Registry, TreeValidator, ValidatorConfig

from orchestrator import agent_backends
from orchestrator.agent import Agent, AgentConfig, AgentSession, decision_outputs, offset_goal
from orchestrator.agent_backends import OpenAICompatibleAgentBackend, ScriptedAgentBackend
from orchestrator.agent_external import ExternalAgentBackend
from orchestrator.agent_sandbox import SandboxConfig, run_python
from orchestrator.behavior_tree import BehaviorTree, Context, Status
from orchestrator.skill_leaf import make_leaf_factory


ROOT       = Path(__file__).resolve().parents[2]
CONTRACTS  = ROOT / 'skill_registry' / 'contracts'
SKILLS_DIR = ROOT / 'skill_registry' / 'agent_skills'
TREES      = ROOT / 'skill_registry' / 'trees'
PANEL      = {'x': 1.0, 'y': 2.0, 'yaw': math.pi / 2, 'frame': 'map'}


class FakeHandle:
    def __init__(self, status: Status, message: str = '', outputs: dict | None = None, feedback: list | None = None):
        self.status, self.message, self.outputs, self.feedback = status, message, outputs or {}, feedback or []

    def poll(self) -> Status:
        return self.status

    def cancel(self):
        pass


class FakeRunner:
    """press_button plays the given results in order; everything else succeeds"""

    def __init__(self, presses: list[tuple], failing: tuple = ()):
        self.presses = list(presses)
        self.failing = set(failing)             # skills that fail every time, e.g. button_lit
        self.calls   = []

    def start(self, contract, inputs):
        self.calls.append((contract.name, inputs))

        if contract.name == 'press_button':
            success, message, feedback = self.presses.pop(0)
            return FakeHandle(Status.SUCCESS if success else Status.FAILURE, message, feedback=feedback)

        if contract.name == 'decision_is':
            return FakeHandle(Status.SUCCESS if inputs['value'] == inputs['equals'] else Status.FAILURE)

        if contract.name in self.failing:
            return FakeHandle(Status.FAILURE, f'{contract.name} -> no')

        return FakeHandle(Status.SUCCESS)


STALLED = [{'t': 0.5 * index, 'distance_to_goal': max(0.13, 0.45 - 0.1 * index), 'tracking_lost': False} for index in range(8)]
MISS    = (False, 'Planning failed: empty trajectory', STALLED)


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(skills_dir=str(SKILLS_DIR), read_roots=[str(ROOT)], sandbox=SandboxConfig(scratch_dir=str(tmp_path / 'scratch')))


def _run(tree: BehaviorTree, max_ticks: int = 500) -> Status:
    for _ in range(max_ticks):
        status = tree.tick()

        if status != Status.RUNNING:
            return status

        time.sleep(0.005)                       # the agent runs in its own thread

    return Status.RUNNING


def _tree(xml_text: str, runner: FakeRunner, agent, scratch_dir: Path | None = None) -> BehaviorTree:
    context = Context(blackboard={'call_panel': dict(PANEL)}, scratch_dir=scratch_dir and str(scratch_dir))

    return BehaviorTree(xml_text, make_leaf_factory(Registry.from_directory(CONTRACTS), runner, agent), context)


# ========== Pieces ==========

def test_offset_goal_moves_in_the_goal_frame():
    goal = offset_goal(PANEL, dx=0.2, dy=0.1, dyaw=0.1)

    assert goal['x'] == pytest.approx(1.0 - 0.1)            # yaw 90 deg: forward is +y, left is -x
    assert goal['y'] == pytest.approx(2.0 + 0.2)
    assert goal['yaw'] == pytest.approx(math.pi / 2 + 0.1)


def test_decision_outputs_keep_the_arm_unless_switched():
    failure = {'inputs': {'left_arm': True}}
    retry   = decision_outputs({'decision': 'retry', 'reason': 'r'}, {'base_goal': PANEL}, failure)
    switch  = decision_outputs({'decision': 'switch_arm', 'reason': 'r', 'use_left_arm': False}, {'base_goal': PANEL}, failure)

    assert retry['left_arm'] is True and retry['goal'] == PANEL
    assert switch['left_arm'] is False


def test_decide_checks_the_phase_and_the_fields(tmp_path):
    session = AgentSession('view_check', {}, {}, None, _config(tmp_path), lambda *a, **k: None)

    assert session.call('decide', {'decision': 'reposition', 'reason': 'x'}).is_error
    assert session.call('decide', {'decision': 'continue', 'reason': ''}).is_error
    assert session.call('decide', {'decision': 'continue', 'reason': 'button in view'}).final
    assert session.call('decide', {'decision': 'abort', 'reason': 'again'}).is_error

    diagnose = AgentSession('diagnose', {'base_goal': PANEL}, {}, None, _config(tmp_path), lambda *a, **k: None)

    assert diagnose.call('decide', {'decision': 'reposition', 'reason': 'far', 'dx': 0.2}).is_error   # no dy
    assert diagnose.call('decide', {'decision': 'reposition', 'reason': 'far', 'dx': 0.2, 'dy': 0.0}).final


def test_read_file_stays_inside_its_roots(tmp_path):
    session = AgentSession('diagnose', {}, {}, None, _config(tmp_path), lambda *a, **k: None)

    assert '1\t---' in session.call('read_file', {'path': str(SKILLS_DIR / 'manipulation_recovery.md'), 'limit': 1}).text
    assert session.call('read_file', {'path': '/etc/passwd'}).is_error


# ========== Sandbox ==========

def test_run_python_runs_code_and_blocks_ros_writes_and_processes(tmp_path):
    config = SandboxConfig(scratch_dir=str(tmp_path / 'scratch'), timeout_s=20)

    assert run_python('import numpy; print(numpy.ones(2).sum())', config)['stdout'].strip() == '2.0'

    for code, error in [
        ('import rclpy', 'blocked in the agent sandbox'),
        ('import unitree_sdk2py', 'blocked in the agent sandbox'),
        (f'open({str(tmp_path / "outside.txt")!r}, "w")', 'writes only under'),
        ('import subprocess; subprocess.run(["true"])', 'subprocess.Popen is blocked'),
        ('import socket; socket.create_connection(("127.0.0.1", 9))', 'no network'),
    ]:
        result = run_python(code, config)
        assert result['exit_code'] != 0 and error in result['stderr'], code

    assert run_python('open("helper.py", "w").write("X = 3")', config)['exit_code'] == 0
    assert run_python('import helper; print(helper.X)', config)['stdout'].strip() == '3'
    assert run_python('while True: pass', SandboxConfig(scratch_dir=config.scratch_dir, timeout_s=1))['timed_out']


# ========== In a tree ==========

def test_a_failed_action_is_recorded_with_its_feedback():
    runner  = FakeRunner([MISS])
    tree    = _tree('<root BTCPP_format="4"><BehaviorTree ID="t"><press_button target_description="up"/></BehaviorTree></root>', runner, None)

    assert _run(tree) == Status.FAILURE
    assert tree.context.last_failure['message'] == 'Planning failed: empty trajectory'
    assert tree.context.last_failure['feedback'][-1]['distance_to_goal'] == pytest.approx(0.13)
    assert 'feedback' not in tree.context.history[-1]


def test_a_failed_check_is_recorded_with_the_call_it_checked():
    pressed = (True, 'Controller reached target', STALLED)
    runner  = FakeRunner([pressed], failing=('button_lit', 'door_open'))
    body    = ('<root BTCPP_format="4"><BehaviorTree ID="t"><Sequence>'
               '<press_button target_description="up" left_arm="true"/><button_lit target_description="up"/>'
               '</Sequence></BehaviorTree></root>')
    tree    = _tree(body, runner, None)

    assert _run(tree) == Status.FAILURE

    failure = tree.context.last_failure

    assert failure['skill'] == 'button_lit' and failure['checks'] == 'press_button'
    assert failure['checked_call']['status'] == 'success' and failure['checked_call']['feedback']
    assert decision_outputs({'decision': 'retry', 'reason': 'r'}, {}, failure)['left_arm'] is True


def test_polled_conditions_are_not_recorded():
    runner = FakeRunner([], failing=('door_open',))
    body   = ('<root BTCPP_format="4"><BehaviorTree ID="t"><RetryUntilSuccessful num_attempts="5">'
              '<door_open/></RetryUntilSuccessful></BehaviorTree></root>')
    tree   = _tree(body, runner, None)

    assert _run(tree) == Status.FAILURE
    assert tree.context.history == [] and tree.context.last_failure is None


def test_reposition_walks_to_the_offset_goal_checks_the_view_and_presses_again(tmp_path):
    backend = ScriptedAgentBackend([
        [['get_failure', {}], ['decide', {'decision': 'reposition', 'reason': 'stalled 0.13 m short', 'dx': 0.15, 'dy': 0.0}]],
        [['decide', {'decision': 'continue', 'reason': 'button in view'}]],
    ])
    runner  = FakeRunner([MISS, (True, 'Controller reached target', [])])
    tree    = _tree((TREES / 'call_elevator_agentic.xml').read_text(), runner, Agent(backend, _config(tmp_path)))

    assert _run(tree) == Status.SUCCESS

    walks = [inputs['goal'] for name, inputs in runner.calls if name == 'navigate_to']

    assert len(walks) == 2 and walks[1]['y'] == pytest.approx(2.15)
    assert 'Planning failed' in backend.calls[0][2].text                   # the agent saw the recorded failure
    assert [record['decision'] for record in tree.context.history if 'decision' in record] == ['reposition', 'continue']


def test_agent_calls_in_one_run_share_its_scratch_folder(tmp_path):
    class TaskBackend(ScriptedAgentBackend):
        def run(self, system, task, *arguments, **keywords):
            self.tasks = [*getattr(self, 'tasks', []), task]
            return super().run(system, task, *arguments, **keywords)

    backend = TaskBackend([
        [['run_python', {'code': 'open("plan.md", "w").write("stalled short: walk 0.15 m closer")'}],
         ['decide', {'decision': 'reposition', 'reason': 'stalled 0.13 m short', 'dx': 0.15, 'dy': 0.0}]],
        [['decide', {'decision': 'continue', 'reason': 'button in view'}]],
    ])
    runner  = FakeRunner([MISS, (True, 'Controller reached target', [])])
    scratch = tmp_path / 'scratch' / 'r1'
    scratch.mkdir(parents=True)
    tree    = _tree((TREES / 'call_elevator_agentic.xml').read_text(), runner, Agent(backend, _config(tmp_path)), scratch)

    assert _run(tree) == Status.SUCCESS
    assert (scratch / 'plan.md').exists()
    assert 'It is empty' in backend.tasks[0]
    assert 'Earlier calls left: plan.md.' in backend.tasks[1]              # not the hidden code file of run_python


def test_abort_ends_the_tree_at_once(tmp_path):
    backend = ScriptedAgentBackend([[['decide', {'decision': 'abort', 'reason': 'button not in view'}]]])
    runner  = FakeRunner([MISS])
    tree    = _tree((TREES / 'call_elevator_agentic.xml').read_text(), runner, Agent(backend, _config(tmp_path)))

    assert _run(tree) == Status.FAILURE
    assert 'button not in view' in tree.context.abort_reason
    assert 'say' not in [name for name, _ in runner.calls]                 # no fallback ran after the abort


def test_after_ask_help_the_agent_is_not_called_again(tmp_path):
    backend = ScriptedAgentBackend([[['decide', {'decision': 'ask_help', 'reason': 'wrong object tracked'}]]])
    runner  = FakeRunner([MISS])
    tree    = _tree((TREES / 'call_elevator_agentic.xml').read_text(), runner, Agent(backend, _config(tmp_path)))

    assert _run(tree) == Status.SUCCESS                                    # a person is asked; the door then opens
    assert len(backend.calls) == 1
    assert 'say' in [name for name, _ in runner.calls]


def test_the_round_budget_stops_the_agent(tmp_path):
    retry   = [['decide', {'decision': 'retry', 'reason': 'try again'}]]
    backend = ScriptedAgentBackend([retry])
    runner  = FakeRunner([MISS, MISS])
    body    = ('<root BTCPP_format="4"><BehaviorTree ID="t"><RetryUntilSuccessful num_attempts="3"><Sequence>'
               '<manipulation_recovery max_rounds="2" base_goal="{call_panel}"/><press_button target_description="up"/>'
               '</Sequence></RetryUntilSuccessful></BehaviorTree></root>')
    tree    = _tree(body, runner, Agent(backend, _config(tmp_path)))
    tree.context.last_failure = {'inputs': {}}

    assert _run(tree) == Status.FAILURE
    assert len(backend.calls) == 2


def test_the_agentic_tree_validates_online_only():
    registry = Registry.from_directory(CONTRACTS)
    xml_text = (TREES / 'call_elevator_agentic.xml').read_text()

    assert TreeValidator(registry).is_valid(xml_text)
    assert not TreeValidator(registry, ValidatorConfig(blind=True)).is_valid(xml_text)


# ========== OpenAI-compatible backend ==========

def test_openai_compatible_backend_sends_tool_results_back(monkeypatch, tmp_path):
    replies = [
        {'tool_calls': [{'id': 'a', 'type': 'function', 'function': {'name': 'get_history', 'arguments': '{}'}}]},
        {'tool_calls': [{'id': 'b', 'type': 'function', 'function': {'name': 'decide', 'arguments': json.dumps({'decision': 'retry', 'reason': 'transient'})}}]},
    ]
    sent = []

    def fake_post(base_url, body, api_key, timeout_s):
        sent.append(json.loads(json.dumps(body)))
        return {'choices': [{'message': {'role': 'assistant', 'content': '', **replies.pop(0)}}]}

    monkeypatch.setattr(agent_backends, 'post_chat_completions', fake_post)

    session = AgentSession('diagnose', {}, {'history': [{'skill': 'navigate_to'}]}, None, _config(tmp_path), lambda *a, **k: None)
    OpenAICompatibleAgentBackend('http://thor', 'local').run('system', 'task', [], session.call, 5, lambda: False, lambda *a, **k: None)

    assert session.decision['decision'] == 'retry'
    assert sent[1]['messages'][-1]['role'] == 'tool' and 'navigate_to' in sent[1]['messages'][-1]['content']


# ========== External backend (a session answers through agent_cli) ==========

def test_external_backend_waits_checks_and_closes_the_call(tmp_path):
    requests, replies = [], []
    backend = ExternalAgentBackend(requests.append, replies.append)
    runner  = FakeRunner([MISS])
    tree    = _tree((TREES / 'call_elevator_agentic.xml').read_text(), runner, Agent(backend, _config(tmp_path)))

    for _ in range(400):
        tree.tick()

        if requests:
            break

        time.sleep(0.005)

    request = requests[-1]

    assert request['state'] == 'open' and request['skill'] == 'manipulation_recovery'
    assert request['failure']['message'] == 'Planning failed: empty trajectory' and request['failure']['feedback']
    assert 'continue' not in request['decisions']

    backend.deliver({'id': 'other', 'decision': 'retry', 'reason': 'r'})
    backend.deliver({'id': request['id'], 'decision': 'continue', 'reason': 'wrong phase'})
    backend.deliver({'id': request['id'], 'decision': 'ask_help', 'reason': 'the session wants a person'})

    assert _run(tree) == Status.SUCCESS                                    # say, then the door opens
    assert [reply['accepted'] for reply in replies] == [False, False, True]
    assert requests[-1]['state'] == 'closed'
    assert 'say' in [name for name, _ in runner.calls]


def test_say_speaks_short_sentences_spaced_out(tmp_path):
    spoken, now = [], [0.0]
    session = AgentSession('diagnose', {}, {}, None, _config(tmp_path), lambda *a, **k: None, spoken.append, lambda: now[0])

    assert not session.call('say', {'text': 'Checking   the  reach.'}).is_error
    assert session.call('say', {'text': 'Too soon.'}).is_error                 # under 3 s after the last one
    now[0] = 3.5
    assert session.call('say', {'text': 'x' * 201}).is_error                   # too long
    assert not session.call('say', {'text': 'Stepping back 20 centimetres.'}).is_error
    assert spoken == ['Checking the reach.', 'Stepping back 20 centimetres.']

    assert AgentSession('diagnose', {}, {}, None, _config(tmp_path), lambda *a, **k: None).call('say', {'text': 'hi'}).is_error


# ========== press_judge: the check after a press, with an agent in the loop ==========

JUDGE_TREE = '''<root BTCPP_format="4"><BehaviorTree ID="t"><Sequence>
  <press_button target_description="floor 1"/>
  <Fallback>
    <press_judge target_description="floor 1"/>
    <manipulation_recovery phase="diagnose" target_description="floor 1" base_goal="{call_panel}" decision="{decision}"/>
  </Fallback>
</Sequence></BehaviorTree></root>'''
PRESS_EVIDENCE = {'left_arm': False, 'goal': [0.4, -0.1, 1.0],
                  'capture': {'max_depth_m': -0.003, 'lateral_at_max_m': 0.004},
                  'frames': {'press': '/tmp/press_1.000_press.jpg', 'baseline': '/tmp/press_0.500_baseline.jpg'}}


def _wait_request(tree, requests, count: int):
    for _ in range(400):
        tree.tick()

        if len([request for request in requests if request['state'] == 'open']) >= count:
            return [request for request in requests if request['state'] == 'open'][-1]

        time.sleep(0.005)

    raise AssertionError('no request')


def test_press_judge_sends_the_press_frames_and_pressed_goes_on(tmp_path):
    requests, replies = [], []
    backend = ExternalAgentBackend(requests.append, replies.append)
    agent   = Agent(backend, _config(tmp_path), press_evidence=lambda: PRESS_EVIDENCE)
    runner  = FakeRunner([(True, 'Controller reached target', [])])
    tree    = _tree(JUDGE_TREE, runner, agent)

    request = _wait_request(tree, requests, 1)

    assert request['skill'] == 'press_judge'
    assert request['decisions'] == ['pressed', 'not_pressed']
    assert request['press']['frames']['press'].endswith('_press.jpg')

    backend.deliver({'id': request['id'], 'decision': 'pressed', 'reason': 'the fingertip covers the button'})

    assert _run(tree) == Status.SUCCESS
    assert not [r for r in requests if r['skill'] == 'manipulation_recovery']


def test_not_pressed_is_the_failed_check_of_the_press_for_the_recovery(tmp_path):
    requests, replies = [], []
    backend = ExternalAgentBackend(requests.append, replies.append)
    agent   = Agent(backend, _config(tmp_path), press_evidence=lambda: PRESS_EVIDENCE)
    runner  = FakeRunner([(True, 'Controller reached target', [{'t': 0.5, 'distance_to_goal': 0.01}])])
    tree    = _tree(JUDGE_TREE, runner, agent)

    judge = _wait_request(tree, requests, 1)
    backend.deliver({'id': judge['id'], 'decision': 'not_pressed', 'reason': 'the finger sits beside the button'})

    recovery = _wait_request(tree, requests, 2)

    assert recovery['skill'] == 'manipulation_recovery'
    assert recovery['failure']['skill'] == 'press_judge' and recovery['failure']['checks'] == 'press_button'
    assert recovery['failure']['checked_call']['skill'] == 'press_button'
    assert recovery['failure']['checked_call']['feedback']
    assert {'elapsed_s', 'status', 'message'} <= set(recovery['failure'])      # the executor's trace formats these

    backend.deliver({'id': recovery['id'], 'decision': 'ask_help', 'reason': 'r'})
    assert _run(tree) == Status.FAILURE



# ========== offset_retry ==========

def test_offset_retry_needs_the_whole_offset_within_30_mm(tmp_path):
    session = AgentSession('diagnose', {}, {}, None, _config(tmp_path), lambda *a, **k: None)

    assert session.call('decide', {'decision': 'offset_retry', 'reason': 'r', 'ox': 0.005}).is_error
    assert session.call('decide', {'decision': 'offset_retry', 'reason': 'r', 'ox': 0.05, 'oy': 0.0, 'oz': 0.0}).is_error
    assert not session.call('decide', {'decision': 'offset_retry', 'reason': 'rim', 'ox': 0.005, 'oy': 0.0075, 'oz': 0.0034}).is_error

    outputs = decision_outputs(session.decision, {}, {'inputs': {'left_arm': False}})

    assert (outputs['offset_x'], outputs['offset_y'], outputs['offset_z']) == (0.005, 0.0075, 0.0034)
    assert outputs['left_arm'] is False



OFFSET_TREE = '''<root BTCPP_format="4"><BehaviorTree ID="t"><Fallback>
  <Sequence>
    <press_button target_description="floor 1"/>
    <press_judge target_description="floor 1"/>
  </Sequence>
  <Sequence>
    <manipulation_recovery phase="diagnose" target_description="floor 1" base_goal="{call_panel}" decision="{decision}"
                           offset_x="{offset_x}" offset_y="{offset_y}" offset_z="{offset_z}"/>
    <Fallback>
      <Inverter><decision_is value="{decision}" equals="offset_retry"/></Inverter>
      <set_press_offset x="{offset_x}" y="{offset_y}" z="{offset_z}"/>
    </Fallback>
    <press_button target_description="floor 1"/>
    <press_judge target_description="floor 1"/>
  </Sequence>
</Fallback></BehaviorTree></root>'''


def test_offset_retry_sets_the_offset_and_presses_again(tmp_path):
    requests, replies = [], []
    backend = ExternalAgentBackend(requests.append, replies.append)
    agent   = Agent(backend, _config(tmp_path), press_evidence=lambda: PRESS_EVIDENCE)
    runner  = FakeRunner([(True, 'Controller reached target', [{'t': 0.5}]), (True, 'Controller reached target', [])])
    tree    = _tree(OFFSET_TREE, runner, agent)

    judge = _wait_request(tree, requests, 1)
    backend.deliver({'id': judge['id'], 'decision': 'not_pressed', 'reason': 'on the rim'})
    recovery = _wait_request(tree, requests, 2)
    assert 'offset_retry' in recovery['decisions']
    backend.deliver({'id': recovery['id'], 'decision': 'offset_retry', 'reason': 'rim', 'ox': 0.005, 'oy': 0.0075, 'oz': 0.0034})
    again = _wait_request(tree, requests, 3)
    backend.deliver({'id': again['id'], 'decision': 'pressed', 'reason': 'lit'})

    assert _run(tree) == Status.SUCCESS
    offsets = [inputs for name, inputs in runner.calls if name == 'set_press_offset']
    assert offsets == [{'x': 0.005, 'y': 0.0075, 'z': 0.0034}]
    assert [name for name, _ in runner.calls].count('press_button') == 2



def test_the_session_gets_a_long_failure_whole(tmp_path):
    """A 60 s press keeps 120 feedback samples, over the 40 kB a model's tool output is cut to"""
    requests, replies = [], []
    backend  = ExternalAgentBackend(requests.append, replies.append)
    goal     = {'header': {'frame_id': 'foot_world'}, 'pose': {'position': {'x': 0.4, 'y': -0.1, 'z': 1.0}}}
    feedback = [{'t': 0.5 * index, 'distance_to_goal': float('inf'), 'tracking_lost': False, 'current_goal': goal,
                 'padding': 'x' * 300} for index in range(120)]
    runner   = FakeRunner([(False, 'timeout after 60 s', feedback)])
    tree     = _tree((TREES / 'call_elevator_agentic.xml').read_text(), runner, Agent(backend, _config(tmp_path)))

    request = _wait_request(tree, requests, 1)

    assert request['failure'] is not None and len(request['failure']['feedback']) == 120
    backend.deliver({'id': request['id'], 'decision': 'ask_help', 'reason': 'r'})
    _run(tree)



def test_a_literal_pose_can_carry_the_storey():
    from orchestrator.skill_leaf import parse_pose

    assert parse_pose('3.74 -0.422 2.67') == {'x': 3.74, 'y': -0.422, 'yaw': 2.67, 'frame': 'map'}
    assert parse_pose('3.74, -0.422, -0.435, 2.67')['z'] == -0.435
