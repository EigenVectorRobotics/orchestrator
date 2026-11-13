import json
import math
import time

from pathlib import Path

import pytest

from skill_registry import Registry, load_tree

from orchestrator import agent_backends
from orchestrator.agent import DECISION_SPEECH, PHASE_SPEECH, Agent, AgentConfig, AgentSession, decision_outputs, offset_goal, rule_offset
from orchestrator.agent_backends import OpenAICompatibleAgentBackend, ScriptedAgentBackend
from orchestrator.agent_external import ExternalAgentBackend
from orchestrator.agent_sandbox import SandboxConfig, run_python
from orchestrator.behavior_tree import BehaviorTree, Context, Status
from orchestrator.skill_leaf import make_leaf_factory


ROOT       = Path(__file__).resolve().parents[2]
CONTRACTS  = ROOT / 'skill_registry' / 'contracts'
SKILLS_DIR = ROOT / 'skill_registry' / 'agent_skills'
TREES      = ROOT / 'orchestrator' / 'test_trees'
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


def _tree(xml_text: str, runner: FakeRunner, agent) -> BehaviorTree:
    context = Context(blackboard={'call_panel': dict(PANEL)})

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
    session = AgentSession('stance_check', {'base_goal': PANEL}, {}, None, _config(tmp_path), lambda *a, **k: None)

    assert session.call('decide', {'decision': 'reposition', 'reason': 'x'}).is_error                  # no dx, dy
    assert session.call('decide', {'decision': 'switch_arm', 'reason': 'x', 'use_left_arm': True}).is_error
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


def test_rule_answers_do_not_spend_the_round_budget(tmp_path):
    retry   = [['decide', {'decision': 'retry', 'reason': 'try again'}]]
    backend = ScriptedAgentBackend([retry])
    body    = ('<root BTCPP_format="4"><BehaviorTree ID="t">'
               '<manipulation_recovery max_rounds="2" base_goal="{call_panel}"/></BehaviorTree></root>')
    by_rule = {'skill': 'manipulation_recovery', 'phase': 'diagnose', 'decision': 'offset_retry', 'rule': True}
    by_agent = {**by_rule, 'decision': 'retry', 'rule': False}

    tree = _tree(body, FakeRunner([]), Agent(backend, _config(tmp_path)))
    tree.context.last_failure, tree.context.history = {'inputs': {}}, [by_rule, by_rule, by_agent]
    assert _run(tree) == Status.SUCCESS and len(backend.calls) == 1        # 1 agent diagnosis of 2 spent

    tree = _tree(body, FakeRunner([]), Agent(backend, _config(tmp_path)))
    tree.context.last_failure, tree.context.history = {'inputs': {}}, [by_rule, by_agent, by_agent]
    assert _run(tree) == Status.FAILURE and len(backend.calls) == 1        # 2 agent diagnoses: the budget is spent


def test_the_executor_says_when_a_call_opens_and_what_was_decided(tmp_path):
    spoken  = []
    backend = ScriptedAgentBackend([[['decide', {'decision': 'retry', 'reason': 'a transient rejection'}]]])
    body    = ('<root BTCPP_format="4"><BehaviorTree ID="t">'
               '<manipulation_recovery base_goal="{call_panel}"/></BehaviorTree></root>')
    tree    = _tree(body, FakeRunner([]), Agent(backend, _config(tmp_path), speak=spoken.append))
    tree.context.last_failure = {'inputs': {}}

    assert _run(tree) == Status.SUCCESS
    assert spoken == [PHASE_SPEECH['diagnose'], DECISION_SPEECH['retry']]


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


# ========== say ==========

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
    assert recovery['press'] == PRESS_EVIDENCE                                 # the depth and residual of the judged press

    backend.deliver({'id': recovery['id'], 'decision': 'ask_help', 'reason': 'r'})
    assert _run(tree) == Status.FAILURE


# ========== offset_retry ==========

def test_offset_retry_needs_the_whole_offset_within_50_mm(tmp_path):
    session = AgentSession('diagnose', {}, {}, None, _config(tmp_path), lambda *a, **k: None)

    assert session.call('decide', {'decision': 'offset_retry', 'reason': 'r', 'ox': 0.005}).is_error
    assert session.call('decide', {'decision': 'offset_retry', 'reason': 'r', 'ox': 0.06, 'oy': 0.0, 'oz': 0.0}).is_error
    assert not session.call('decide', {'decision': 'offset_retry', 'reason': 'rim', 'ox': 0.005, 'oy': 0.0075, 'oz': 0.0034}).is_error

    outputs = decision_outputs(session.decision, {}, {'inputs': {'left_arm': False}})

    assert (outputs['offset_x'], outputs['offset_y'], outputs['offset_z']) == (0.005, 0.0075, 0.0034)
    assert outputs['left_arm'] is False


def test_rule_offset_starts_from_the_offset_the_press_used():
    """One set_press_offset, floor 2 judged pressed, then floor 1 missed with no new set_press_offset: the offset in
    force is still the inside one, though no set_press_offset follows the last pressed"""
    from orchestrator.agent import rule_offset

    history  = [{'skill': 'set_press_offset', 'inputs': {'x': 0.005, 'y': 0.0075, 'z': 0.0034}},
                {'skill': 'press_judge', 'decision': 'pressed'}]
    failure  = {'skill': 'press_judge', 'checked_call': {'skill': 'press_button'}}
    residual = [-0.002, 0.008, 0.0]

    rule = rule_offset(failure, history, residual, [0.005, 0.0075, 0.0034])
    assert (rule['ox'], rule['oy'], rule['oz']) == pytest.approx((0.007, -0.0005, 0.0034))

    guessed = rule_offset(failure, history, residual)                  # from the history alone: taken as 0
    assert (guessed['ox'], guessed['oy'], guessed['oz']) == pytest.approx((0.002, -0.008, 0.0))


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


def test_a_literal_pose_can_carry_the_storey():
    from orchestrator.skill_leaf import parse_pose

    assert parse_pose('3.74 -0.422 2.67') == {'x': 3.74, 'y': -0.422, 'yaw': 2.67, 'frame': 'map'}
    assert parse_pose('3.74, -0.422, -0.435, 2.67')['z'] == -0.435


# ========== stance_check: before the first press ==========

def test_stance_check_decisions(tmp_path):
    session = AgentSession('stance_check', {'base_goal': PANEL}, {}, None, _config(tmp_path), lambda *a, **k: None)

    assert session.call('decide', {'decision': 'offset_retry', 'reason': 'x', 'ox': 0, 'oy': 0, 'oz': 0}).is_error
    assert session.call('decide', {'decision': 'ask_help', 'reason': 'x'}).is_error
    assert session.call('decide', {'decision': 'switch_arm', 'reason': 'x', 'use_left_arm': True}).is_error   # the arm is chosen by rule
    assert session.call('decide', {'decision': 'reposition', 'reason': 'too close', 'dx': -0.25, 'dy': 0.0}).final


def test_a_failed_stance_check_is_recorded_for_the_agent():
    runner = FakeRunner([], failing=('button_in_view',))
    body   = ('<root BTCPP_format="4"><BehaviorTree ID="t"><Sequence><button_in_view query="down button"/>'
              '</Sequence></BehaviorTree></root>')
    tree   = _tree(body, runner, None)

    assert _run(tree) == Status.FAILURE
    assert tree.context.last_failure['skill'] == 'button_in_view'
    assert tree.context.last_failure['inputs'] == {'query': 'down button'}


# ========== confirm ===
def test_confirm_phase_relays_the_question():
    from orchestrator.agent import GOING_ON, PHASE_DECISIONS, _task

    assert PHASE_DECISIONS['confirm'] == ('confirmed', 'not_confirmed')
    assert 'confirmed' in GOING_ON and 'not_confirmed' not in GOING_ON
    assert 'Is the gantry removed?' in _task('confirm', {'question': 'Is the gantry removed?'})


# ========== The library ride tree with the press checks ==========

LIBRARY_RIDE  = ROOT / 'skill_registry' / 'trees' / 'freight_elevator_go_to_place.xml'
LIBRARY_PLACES = ('freight_elevator_call_panel_floor_1', 'inside_freight_elevator_button_panel_floor_1',
                  'freight_elevator_door_out_floor_2', 'target_floor_2')


class LibraryRideRunner(FakeRunner):
    """The robot starts on floor 1. The door opens once the call press is judged, the cabin is reached by the walk in, and
    floor 2 comes (door open) once the floor press is judged. judges: press_judge results in order (True pressed); stance:
    the results of button_in_view in order (True in view); hold_judge: the first press_judge stays running (the door
    opens meanwhile)."""

    def __init__(self, presses: list[tuple], judges: list[bool] = (), stance: list[bool] = (), decisions: list[dict] = (),
                 hold_judge: bool = False, arms: list[bool] = ()):
        super().__init__(presses)
        self.arms = list(arms)
        self.judges, self.stance, self.decisions = list(judges), list(stance), list(decisions)
        self.hold_judge, self.door, self.inside, self.floor = hold_judge, False, False, '1'

    def start(self, contract, inputs):
        name = contract.name

        if name in ('door_open', 'inside_cabin', 'at_floor'):
            held = {'door_open': self.door, 'inside_cabin': self.inside, 'at_floor': str(inputs.get('floor')) == self.floor}[name]
            return FakeHandle(Status.SUCCESS if held else Status.FAILURE)

        if name == 'in_elevator':
            return FakeHandle(Status.FAILURE)                   # LIO has left elevator mode

        if name not in ('manipulation_recovery', 'press_judge', 'button_in_view'):
            return self._other(name, contract, inputs)

        self.calls.append((name, inputs))

        if name == 'button_in_view':
            in_view  = self.stance.pop(0) if self.stance else True
            left_arm = self.arms.pop(0) if self.arms and in_view else False
            return FakeHandle(Status.SUCCESS if in_view else Status.FAILURE, 'in view' if in_view else 'not in view',
                              {'left_arm': left_arm})

        if name == 'manipulation_recovery':
            decision = self.decisions.pop(0) if self.decisions else {'decision': 'continue', 'left_arm': False}
            return FakeHandle(Status.SUCCESS if decision['decision'] != 'abort' else Status.FAILURE, decision['decision'],
                              {'reason': 'test', **decision})

        if name == 'press_judge':
            if self.hold_judge:
                self.hold_judge, self.door = False, True            # the door opens while the session judges
                return FakeHandle(Status.RUNNING)

            pressed = self.judges.pop(0) if self.judges else True

            if pressed and inputs['target_description'] == 'down button':
                self.door = True
            if pressed and inputs['target_description'] == 'floor 2':
                self.floor, self.door = '2', True

            decision = 'pressed' if pressed else 'not_pressed'
            return FakeHandle(Status.SUCCESS if pressed else Status.FAILURE, decision, {'decision': decision, 'reason': 'test'})

    def _other(self, name, contract, inputs):
        if name == 'navigate_to' and inputs['goal']['x'] == 3.0:    # the inside panel: the robot is in the cabin
            self.inside, self.door = True, False

        return super().start(contract, inputs)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _library_ride(runner: FakeRunner) -> BehaviorTree:
    now     = [0.0]
    clock   = lambda: now.__setitem__(0, now[0] + 0.5) or now[0]
    places  = {name: {'x': 1.0, 'y': 2.0, 'z': -0.46, 'yaw': 0.0, 'frame': 'map'} for name in LIBRARY_PLACES}
    places['inside_freight_elevator_button_panel_floor_1']['x'] = 3.0
    context = Context(blackboard=places, clock=clock)

    agent   = type('FakeAgent', (), {'start': lambda self, contract, inputs, context: runner.start(contract, inputs)})()

    return BehaviorTree(load_tree(LIBRARY_RIDE), make_leaf_factory(Registry.from_directory(CONTRACTS), runner, agent), context)


def _press_steps(names: list[str]) -> list[str]:
    return [name for name in names if name in ('button_in_view', 'press_button', 'press_judge')]


def test_each_press_checks_the_stance_then_presses_and_judges():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2)

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    assert _press_steps(runner.names()) == ['button_in_view', 'press_button', 'press_judge'] * 2
    assert 'track' not in runner.names()
    presses = [inputs for name, inputs in runner.calls if name == 'press_button']
    assert [press['target_description'] for press in presses] == ['down button', 'floor 2']
    offsets = [inputs for name, inputs in runner.calls if name == 'set_press_offset']
    assert [(o['x'], o['y'], o['z']) for o in offsets] == [(0.0, 0.0, 0.0)] * 2                # no offset before a first press
    says  = [(index, inputs['text']) for index, (name, inputs) in enumerate(runner.calls) if name == 'say']
    holds = [index for index, text in says if text == 'Holding still to reset my balance estimate.']
    assert len(holds) == 2                                                                  # before each press
    assert all(runner.names().index('press_button', index) > index for index in holds)
    assert 'manipulation_recovery' not in runner.names()


def test_a_press_judged_not_pressed_goes_to_manipulation_recovery_and_is_judged_again():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 3, judges=[False, True, True],
                               decisions=[{'decision': 'retry', 'left_arm': False}])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    names    = runner.names()
    recovery = names.index('manipulation_recovery')
    assert runner.calls[recovery][1]['phase'] == 'diagnose'
    assert runner.calls[recovery][1]['base_goal']['x'] == 1.0                              # the call panel, copied as a pose
    assert names[recovery + 1:].index('press_button') < names[recovery + 1:].index('press_judge')


def test_an_offset_retry_presses_again_with_the_offset_the_agent_gave():
    offset = {'decision': 'offset_retry', 'left_arm': False, 'offset_x': 0.012, 'offset_y': 0.0126, 'offset_z': 0.0032}
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 3, judges=[False, True, True], decisions=[offset])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    offsets = [(o['x'], o['y'], o['z']) for name, o in runner.calls if name == 'set_press_offset']
    assert offsets == [(0.0, 0.0, 0.0), (0.012, 0.0126, 0.0032), (0.0, 0.0, 0.0)]   # call press, its retry, the floor press
    assert runner.names().count('press_button') == 3


def test_a_reposition_after_a_press_looks_again_and_presses_without_asking_the_agent():
    goal   = {'decision': 'reposition', 'left_arm': False, 'goal': {'x': 0.7, 'y': 2.0, 'z': -0.46, 'yaw': 0.0}}
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 3, judges=[False, True, True], decisions=[goal])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    phases = [inputs['phase'] for name, inputs in runner.calls if name == 'manipulation_recovery']
    names  = runner.names()
    after  = names[names.index('manipulation_recovery'):]
    assert phases == ['diagnose']                                       # the look after the walk asks no agent
    assert after.index('release_arms') < after.index('navigate_to') < after.index('button_in_view') < after.index('press_button')


def test_a_reposition_whose_three_looks_fail_ends_the_task_without_asking():
    goal   = {'decision': 'reposition', 'left_arm': False, 'goal': {'x': 0.7, 'y': 2.0, 'z': -0.46, 'yaw': 0.0}}
    runner = LibraryRideRunner([(True, 'Controller reached target', [])], judges=[False], decisions=[goal], stance=[True, False, False, False])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.FAILURE
    names = runner.names()
    says  = [inputs['text'] for name, inputs in runner.calls if name == 'say']
    assert names.count('button_in_view') == 4                           # the stance check, then three looks after the reposition
    assert 'Please press the call button for me.' not in says
    assert names.count('press_button') == 1


def test_a_failed_stance_check_keeps_the_arm_the_rule_chose():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2, stance=[False, False],
                               decisions=[{'decision': 'continue', 'left_arm': True}, {'decision': 'continue', 'left_arm': True}])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    stance  = [inputs for name, inputs in runner.calls if name == 'manipulation_recovery']
    presses = [inputs for name, inputs in runner.calls if name == 'press_button']
    assert [call['phase'] for call in stance] == ['stance_check', 'stance_check']
    assert [press['left_arm'] for press in presses] == [False, False]      # the agent's left_arm is not taken

def test_the_arm_button_in_view_chooses_is_the_arm_that_presses():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2, arms=[True, False])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    presses = [inputs for name, inputs in runner.calls if name == 'press_button']
    assert [press['left_arm'] for press in presses] == [True, False]


def _walks_before_first_press(runner) -> int:
    names = runner.names()
    return names[:names.index('press_button')].count('navigate_to')


def test_the_stance_loop_walks_again_after_a_reposition_until_the_button_is_in_view():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2, stance=[False, True],
                               decisions=[{'decision': 'reposition', 'left_arm': False, 'goal': {'x': 0.7, 'y': 2.0, 'z': -0.46, 'yaw': 0.0}}])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    stance = [inputs for name, inputs in runner.calls if name == 'manipulation_recovery']
    walks  = [inputs['goal'] for name, inputs in runner.calls if name == 'navigate_to']
    assert [call['phase'] for call in stance] == ['stance_check']
    assert _walks_before_first_press(runner) == 3                       # the call panel, the stance at offset 0, the reposition
    assert walks[2]['x'] == 0.7


def test_the_stance_loop_gives_up_after_three_repositions_and_asks_for_help():
    goal   = {'decision': 'reposition', 'left_arm': False, 'goal': {'x': 0.7, 'y': 2.0, 'z': -0.46, 'yaw': 0.0}}
    runner = LibraryRideRunner([(True, 'Controller reached target', [])], stance=[False] * 4, decisions=[goal] * 4)
    tree   = _library_ride(runner)

    _run(tree, max_ticks=40000)
    names  = runner.names()
    stance = [inputs for name, inputs in runner.calls if name == 'manipulation_recovery' and inputs['phase'] == 'stance_check']
    says   = [inputs['text'] for name, inputs in runner.calls if name == 'say']
    assert len(stance) == 4                                             # one after each of the four looks
    assert names[:names.index('say', names.index('manipulation_recovery'))].count('navigate_to') == 5   # panel, offset 0, 3 repositions
    assert 'Please press the call button for me.' in says
    assert names.index('release_arms') < names.index('say', names.index('manipulation_recovery'))
    assert 'press_button' not in names[:names.index('release_arms')]    # no blind press at the call panel


def test_a_door_that_opens_during_the_judge_halts_the_press_and_walks_in():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2, hold_judge=True)

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    names = runner.names()
    assert names.count('press_judge') == 2                                                 # the halted one, then floor 1
    assert 'manipulation_recovery' not in names
    assert names.index('release_arms') > names.index('press_judge')


# ========== The offset rule before the agent ==========

JUDGED_MISS = {'skill': 'press_judge', 'status': 'failure', 'checked_call': {'skill': 'press_button', 'inputs': {'left_arm': False}}}
OFFSET_SET  = {'skill': 'set_press_offset', 'inputs': {'x': 0.005, 'y': 0.0075, 'z': 0.0034}}


def test_a_first_miss_in_the_band_is_corrected_without_the_agent():
    rule = rule_offset(JUDGED_MISS, [OFFSET_SET], [-0.002, -0.0051, 0.0002])

    assert rule['decision'] == 'offset_retry'
    assert (rule['ox'], rule['oy'], rule['oz']) == (0.007, 0.0126, 0.0032)          # the offset in force minus the residual


def test_the_rule_leaves_small_large_and_later_misses_to_the_agent():
    assert rule_offset(JUDGED_MISS, [OFFSET_SET], [-0.002, 0.003, 0.002]) is None     # 3.6 mm: inside 5 mm
    assert rule_offset(JUDGED_MISS, [OFFSET_SET], [-0.002, 0.040, 0.035]) is None     # 53 mm: outside 50 mm
    low = {'skill': 'set_press_offset', 'inputs': {'x': 0.0, 'y': 0.0, 'z': -0.02}}
    assert rule_offset(JUDGED_MISS, [low], [-0.002, 0.0, 0.040]) is None              # in the band, but the offset would pass 50 mm
    earlier = {'skill': 'manipulation_recovery', 'phase': 'diagnose', 'decision': 'offset_retry'}
    assert rule_offset(JUDGED_MISS, [OFFSET_SET, earlier], [-0.002, 0.01, 0.0]) is None   # not the first diagnosis


def test_the_rule_applies_again_after_a_press_at_another_panel():
    earlier = [{'skill': 'manipulation_recovery', 'phase': 'diagnose', 'decision': 'retry'},
               {'skill': 'press_judge', 'decision': 'pressed'}, OFFSET_SET]

    assert rule_offset(JUDGED_MISS, earlier, [-0.002, 0.01, 0.0]) is not None


def test_the_rule_needs_a_judged_press_and_a_residual():
    failed_press = {'skill': 'press_button', 'status': 'failure', 'message': 'Planning failed: target out of reach'}

    assert rule_offset(failed_press, [], [-0.002, 0.01, 0.0]) is None
    assert rule_offset(JUDGED_MISS, [], None) is None

