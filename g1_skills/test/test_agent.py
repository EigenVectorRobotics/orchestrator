import math
import time
import queue

from pathlib import Path

import pytest

from skill_registry import Registry, TreeValidator, ValidatorConfig, load_tree

from orchestrator.agent import protocol
from orchestrator.agent.api import AgentCall
from orchestrator.agent.api import Agent, AgentSession, task_text
from orchestrator.agent.link import PushAgentBackend
from orchestrator.tree.engine import BehaviorTree, BTContext, Status
from orchestrator.tree.leaf import make_leaf_factory

from g1_skills.manipulation_recovery.agent import RULE_SPEECH, ManipulationRecovery
from g1_skills.manipulation_recovery.rules import decision_outputs, offset_goal, rule_reach, rule_repress, rule_stance

from fakes import press_ctx


RECOVERY_GOING_ON = protocol.load_spec('manipulation_recovery').GOING_ON


ROOT       = Path(__file__).resolve().parents[2]
CONTRACTS  = ROOT / 'g1_skills' / 'g1_skills'
TREES      = ROOT / 'orchestrator' / 'test_trees'
PANEL      = {'x': 1.0, 'y': 2.0, 'yaw': math.pi / 2, 'frame': 'map'}
AGENTIC    = '''<root BTCPP_format="4">
  <BehaviorTree ID="call_elevator_agentic">
    <Sequence>
      <navigate_to goal="{call_panel}"/>
      <Fallback>
        <press_button target_description="up button"/>
        <RetryUntilSuccessful num_attempts="3">
          <SubTree ID="recover_press"/>
        </RetryUntilSuccessful>
        <say text="Please press the up button for me."/>
      </Fallback>
      <Fallback>
        <RetryUntilSuccessful num_attempts="60">
          <Delay delay_msec="1000">
            <door_open/>
          </Delay>
        </RetryUntilSuccessful>
        <ForceFailure>
          <say text="The elevator door did not open."/>
        </ForceFailure>
      </Fallback>
    </Sequence>
  </BehaviorTree>

  <BehaviorTree ID="recover_press">
    <Sequence>
      <manipulation_recovery phase="diagnose" target_description="up button" base_goal="{call_panel}"
                             decision="{decision}" goal="{recovery_goal}" left_arm="{recovery_left_arm}"/>
      <Fallback>
        <Inverter>
          <decision_is value="{decision}" equals="reposition"/>
        </Inverter>
        <Sequence>
          <navigate_to goal="{recovery_goal}"/>
          <button_in_view query="up button"/>
        </Sequence>
      </Fallback>
      <press_button target_description="up button" left_arm="{recovery_left_arm}"/>
    </Sequence>
  </BehaviorTree>
</root>'''


class FakeHandle:
    def __init__(self, status: Status, message: str = '', outputs: dict | None = None, feedback: list | None = None):
        self.status, self.message, self.outputs, self.feedback = status, message, outputs or {}, feedback or []

    def poll(self) -> Status:
        return self.status

    def cancel(self):
        pass


class FakeRunner:
    """press_button plays the given results in order; everything else succeeds"""

    def __init__(self, presses: list[tuple], failing: tuple = (), agent=None):
        self.presses = list(presses)
        self.failing = set(failing)             # skills that fail every time, e.g. door_open
        self.calls   = []
        self.agent   = agent

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


class ScriptedLink:
    """Stands in for the bridge link: answers each call with the next decision of answers (the last one repeats), or
    leaves it open for answer() when there are none"""

    def __init__(self, answers: list[dict] = ()):
        self.answers   = list(answers)
        self.calls     = []
        self.sent      = []
        self.messages  = queue.Queue()
        self.connected = True

    @property
    def heard_at(self) -> float:
        return time.monotonic()                 # a live bridge: heard from just now

    def open_call(self, header: dict, blobs: list[bytes]):
        while not self.messages.empty():
            self.messages.get_nowait()

        self.calls.append(header)

        if self.answers:
            self.answer(self.answers.pop(0) if len(self.answers) > 1 else self.answers[0])

    def answer(self, decision: dict, call: str | None = None):
        self.messages.put({'type': 'decision', 'id': call or self.calls[-1]['id'], **decision})

    def send(self, header: dict, blobs: list[bytes] = ()) -> bool:
        self.sent.append(header)
        return True

    def close_call(self):
        pass


def _push(tmp_path: Path, answers: list[dict] = (), requests: list | None = None) -> PushAgentBackend:
    return PushAgentBackend(ScriptedLink(answers), (requests if requests is not None else []).append)


def _run(tree: BehaviorTree, max_ticks: int = 500) -> Status:
    for _ in range(max_ticks):
        status = tree.tick()

        if status != Status.RUNNING:
            return status

        time.sleep(0.005)                       # the agent runs in its own thread

    return Status.RUNNING


def _session(phase: str, inputs: dict, evidence: dict | None = None, skill: str = 'manipulation_recovery') -> AgentSession:
    contract = Registry.from_directory(CONTRACTS).get(skill)

    return AgentSession(Agent(None, press_ctx(None)).skill(contract), AgentCall(contract, inputs, BTContext(), phase), evidence or {},
                        lambda *a, **k: None)


def _tree(xml_text: str, runner: FakeRunner, agent) -> BehaviorTree:
    context = BTContext(blackboard={'call_panel': dict(PANEL)})
    runner.agent = agent

    return BehaviorTree(xml_text, make_leaf_factory(Registry.from_directory(CONTRACTS), runner), context)


# ========== Pieces ==========

def test_offset_goal_moves_in_the_goal_frame():
    goal = offset_goal(PANEL, dx=0.2, dy=0.1, dyaw=0.1)

    assert goal['x'] == pytest.approx(1.0 - 0.1)            # yaw 90 deg: forward is +y, left is -x
    assert goal['y'] == pytest.approx(2.0 + 0.2)
    assert goal['yaw'] == pytest.approx(math.pi / 2 + 0.1)


def test_a_reposition_keeps_the_storey_of_its_base_goal():
    panel = {'x': 1.771, 'y': -1.356, 'z': -0.458, 'yaw': math.radians(-15.6), 'frame': 'map'}

    assert offset_goal(panel, dx=-0.2, dy=0.0, dyaw=0.0)['z'] == pytest.approx(-0.458)


def test_decision_outputs_keep_the_arm_unless_switched():
    failure = {'inputs': {'left_arm': True}}
    moved   = decision_outputs({'decision': 'reposition', 'reason': 'r', 'dx': 0.05, 'dy': 0.0}, {'base_goal': PANEL}, failure)
    assert math.isclose(math.dist((moved['goal']['x'], moved['goal']['y']), (PANEL['x'], PANEL['y'])), 0.05)
    assert 'walk_in' not in moved and 'approach' not in moved      # the look asks near_pose before each walk
    retry   = decision_outputs({'decision': 'retry', 'reason': 'r'}, {'base_goal': PANEL}, failure)
    switch  = decision_outputs({'decision': 'switch_arm', 'reason': 'r', 'use_left_arm': False}, {'base_goal': PANEL}, failure)

    assert retry['left_arm'] is True and retry['goal'] == PANEL
    assert switch['left_arm'] is False


def test_decide_checks_the_phase_and_the_fields():
    session = _session('stance_check', {'base_goal': PANEL}, {})

    assert session.decide({'decision': 'reposition', 'reason': 'x'}).is_error                  # no dx, dy
    assert session.decide({'decision': 'switch_arm', 'reason': 'x', 'use_left_arm': True}).is_error
    assert session.decide({'decision': 'continue', 'reason': ''}).is_error
    assert session.decide({'decision': 'continue', 'reason': 'button in view'}).final
    assert session.decide({'decision': 'abort', 'reason': 'again'}).is_error

    diagnose = _session('diagnose', {'base_goal': PANEL}, {})

    assert diagnose.decide({'decision': 'reposition', 'reason': 'far', 'dx': 0.2}).is_error   # no dy
    assert diagnose.decide({'decision': 'reposition', 'reason': 'far', 'dx': 0.1, 'dy': 0.0}).final


# ========== In a tree ==========

def test_a_failed_action_is_recorded_with_its_feedback():
    runner  = FakeRunner([MISS])
    tree    = _tree('<root BTCPP_format="4"><BehaviorTree ID="t"><press_button target_description="up"/></BehaviorTree></root>', runner, None)

    assert _run(tree) == Status.FAILURE
    assert tree.context.last_failure['message'] == 'Planning failed: empty trajectory'
    assert tree.context.last_failure['feedback'][-1]['distance_to_goal'] == pytest.approx(0.13)
    assert 'feedback' not in tree.context.history[-1]


def test_polled_conditions_are_not_recorded():
    runner = FakeRunner([], failing=('door_open',))
    body   = ('<root BTCPP_format="4"><BehaviorTree ID="t"><RetryUntilSuccessful num_attempts="5">'
              '<door_open/></RetryUntilSuccessful></BehaviorTree></root>')
    tree   = _tree(body, runner, None)

    assert _run(tree) == Status.FAILURE
    assert tree.context.history == [] and tree.context.last_failure is None


def test_check_reach_records_every_answer():
    body   = ('<root BTCPP_format="4"><BehaviorTree ID="t"><Sequence><check_reach left_arm="false"/>'
              '<check_reach left_arm="true"/></Sequence></BehaviorTree></root>')
    passed = _tree(body, FakeRunner([]), None)
    failed = _tree(body, FakeRunner([], failing=('check_reach',)), None)

    assert _run(passed) == Status.SUCCESS and [record['status'] for record in passed.context.history] == ['success', 'success']
    assert _run(failed) == Status.FAILURE and failed.context.last_failure['skill'] == 'check_reach'


def test_a_look_resets_the_ekf_after_the_mode_change_and_before_the_check_and_the_press_does_not():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2)

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    names = runner.names()
    press = names.index('press_button')
    assert names.index('set_mode') < names.index('reset_ekf') < names.index('check_reach') < press
    assert names[names.index('check_reach'):press].count('reset_ekf') == 0 and 'set_mode' not in names[names.index('check_reach'):press]


def test_reposition_walks_to_the_offset_goal_checks_the_view_and_presses_again(tmp_path):
    backend = _push(tmp_path, [{'decision': 'reposition', 'reason': 'stalled 0.13 m short', 'dx': 0.15, 'dy': 0.0}])
    runner  = FakeRunner([MISS, (True, 'Controller reached target', [])])
    tree    = _tree(AGENTIC, runner, Agent(backend, press_ctx(None)))

    assert _run(tree) == Status.SUCCESS

    walks = [inputs['goal'] for name, inputs in runner.calls if name == 'navigate_to']

    assert len(walks) == 2 and walks[1]['y'] == pytest.approx(2.15)
    assert 'Planning failed' in backend.link.calls[0]['failure']['message']   # the bridge got the recorded failure
    assert [record['decision'] for record in tree.context.history if 'decision' in record] == ['reposition']
    names = [name for name, _ in runner.calls]
    assert names.index('navigate_to', 1) < names.index('button_in_view') < names.index('press_button', names.index('navigate_to', 1))


LOOK_THEN_CHECK = '''<root BTCPP_format="4">
  <BehaviorTree ID="look_then_check">
    <Sequence>
      <navigate_to goal="{call_panel}"/>
      <Fallback>
        <RetryUntilSuccessful num_attempts="3">
          <Fallback>
            <button_in_view query="up button"/>
            <manipulation_recovery phase="stance_check" target_description="up button" base_goal="{call_panel}" decision="{decision}"/>
          </Fallback>
        </RetryUntilSuccessful>
        <say text="Please press the up button for me."/>
      </Fallback>
    </Sequence>
  </BehaviorTree>
</root>'''


def test_abort_ends_the_tree_at_once(tmp_path):
    backend = _push(tmp_path, [{'decision': 'abort', 'reason': 'button not in view'}])   # a stance_check: diagnose has no abort
    runner  = FakeRunner([], failing=('button_in_view',))
    tree    = _tree(LOOK_THEN_CHECK, runner, Agent(backend, press_ctx(None)))

    assert _run(tree) == Status.FAILURE
    assert 'button not in view' in tree.context.abort_reason
    assert 'say' not in [name for name, _ in runner.calls]                 # no fallback ran after the abort


def test_after_ask_help_the_agent_is_not_called_again(tmp_path):
    backend = _push(tmp_path, [{'decision': 'ask_help', 'reason': 'wrong object tracked'}])
    runner  = FakeRunner([MISS])
    tree    = _tree(AGENTIC, runner, Agent(backend, press_ctx(None)))

    assert _run(tree) == Status.SUCCESS                                    # a person is asked; the door then opens
    assert len(backend.link.calls) == 1
    assert 'say' in [name for name, _ in runner.calls]


def test_an_ask_help_ends_the_calls_for_its_own_panel_only(tmp_path):
    backend = _push(tmp_path, [{'decision': 'ask_help', 'reason': 'wrong object tracked'}, {'decision': 'retry', 'reason': 'r'}])
    body    = ('<root BTCPP_format="4"><BehaviorTree ID="t"><Sequence>'
               '<ForceSuccess><manipulation_recovery base_goal="{call_panel}"/></ForceSuccess>'      # asks for help
               '<manipulation_recovery base_goal="{inside_panel}"/>'                                 # another panel: called
               '<ForceSuccess><manipulation_recovery base_goal="{call_panel}"/></ForceSuccess>'      # the same panel: not called
               '</Sequence></BehaviorTree></root>')
    tree    = BehaviorTree(body, make_leaf_factory(Registry.from_directory(CONTRACTS), FakeRunner([], agent=Agent(backend, press_ctx(None)))),
                           BTContext(blackboard={'call_panel': dict(PANEL), 'inside_panel': {**PANEL, 'x': 3.0}}))
    tree.context.last_failure = {'inputs': {}}

    assert _run(tree) == Status.SUCCESS
    assert len(backend.link.calls) == 2


def test_a_call_that_fails_without_a_decision_says_why(tmp_path):
    class BrokenBackend:
        def run(self, *args, **kwargs):
            raise RuntimeError('the agent bridge failed: no structured output')

    body   = ('<root BTCPP_format="4"><BehaviorTree ID="t">'
              '<manipulation_recovery max_rounds="1" base_goal="{call_panel}"/></BehaviorTree></root>')
    spoken = []
    tree   = _tree(body, FakeRunner([]), Agent(BrokenBackend(), press_ctx(None), speak=spoken.append))
    tree.context.last_failure = {'inputs': {}}

    assert _run(tree) == Status.FAILURE
    assert spoken[-1] == 'My helper could not decide.'

    spent  = {'skill': 'manipulation_recovery', 'phase': 'diagnose', 'decision': 'retry', 'rule': False}
    spoken = []
    tree   = _tree(body, FakeRunner([]), Agent(BrokenBackend(), press_ctx(None), speak=spoken.append))
    tree.context.last_failure, tree.context.history = {'inputs': {}}, [spent]

    assert _run(tree) == Status.FAILURE
    assert spoken == ['I have used up my recovery attempts for this task.']


def test_the_round_budget_stops_the_agent(tmp_path):
    backend = _push(tmp_path, [{'decision': 'retry', 'reason': 'try again'}])
    runner  = FakeRunner([MISS, MISS])
    body    = ('<root BTCPP_format="4"><BehaviorTree ID="t"><RetryUntilSuccessful num_attempts="3"><Sequence>'
               '<manipulation_recovery max_rounds="2" base_goal="{call_panel}"/><press_button target_description="up"/>'
               '</Sequence></RetryUntilSuccessful></BehaviorTree></root>')
    tree    = _tree(body, runner, Agent(backend, press_ctx(None)))
    tree.context.last_failure = {'inputs': {}}

    assert _run(tree) == Status.FAILURE
    assert len(backend.link.calls) == 2


def test_rule_answers_do_not_spend_the_round_budget(tmp_path):
    backend = _push(tmp_path, [{'decision': 'retry', 'reason': 'try again'}])
    body    = ('<root BTCPP_format="4"><BehaviorTree ID="t">'
               '<manipulation_recovery max_rounds="2" base_goal="{call_panel}"/></BehaviorTree></root>')
    by_rule = {'skill': 'manipulation_recovery', 'phase': 'diagnose', 'decision': 'offset_retry', 'rule': True}
    by_agent = {**by_rule, 'decision': 'retry', 'rule': False}

    tree = _tree(body, FakeRunner([]), Agent(backend, press_ctx(None)))
    tree.context.last_failure, tree.context.history = {'inputs': {}}, [by_rule, by_rule, by_agent]
    assert _run(tree) == Status.SUCCESS and len(backend.link.calls) == 1        # 1 agent diagnosis of 2 spent

    tree = _tree(body, FakeRunner([]), Agent(backend, press_ctx(None)))
    tree.context.last_failure, tree.context.history = {'inputs': {}}, [by_rule, by_agent, by_agent]
    assert _run(tree) == Status.FAILURE and len(backend.link.calls) == 1        # 2 agent diagnoses: the budget is spent


def test_the_executor_says_when_a_call_opens_and_what_was_decided(tmp_path):
    spoken  = []
    backend = _push(tmp_path, [{'decision': 'retry', 'reason': 'a transient rejection'}])
    body    = ('<root BTCPP_format="4"><BehaviorTree ID="t">'
               '<manipulation_recovery base_goal="{call_panel}"/></BehaviorTree></root>')
    tree    = _tree(body, FakeRunner([]), Agent(backend, press_ctx(None), speak=spoken.append))
    tree.context.last_failure = {'inputs': {}}

    assert _run(tree) == Status.SUCCESS
    assert spoken == [ManipulationRecovery.phase_speech['diagnose'], ManipulationRecovery.decision_speech['retry']]


def test_the_agentic_tree_validates_online_only():
    registry = Registry.from_directory(CONTRACTS)

    assert TreeValidator(registry).is_valid(AGENTIC)
    assert not TreeValidator(registry, ValidatorConfig(blind=True)).is_valid(AGENTIC)


# ========== The bridge answers over the link ==========

def test_a_call_waits_for_the_bridge_checks_its_decision_and_closes(tmp_path):
    requests = []
    backend  = _push(tmp_path, requests=requests)
    runner   = FakeRunner([MISS])
    tree     = _tree(AGENTIC, runner, Agent(backend, press_ctx(None)))
    call     = _wait_call(tree, backend.link, 1)

    assert requests[0]['state'] == 'open' and call['skill'] == 'manipulation_recovery'
    assert call['failure']['message'] == 'Planning failed: empty trajectory' and call['failure']['feedback']
    assert 'continue' not in call['decisions']

    backend.link.answer({'decision': 'retry', 'reason': 'r'}, 'other')
    backend.link.answer({'decision': 'continue', 'reason': 'wrong phase'})
    backend.link.answer({'decision': 'ask_help', 'reason': 'the bridge wants a person'})

    assert _run(tree) == Status.SUCCESS                                    # say, then the door opens
    assert [reply['accepted'] for reply in backend.link.sent if reply['type'] == 'reply'] == [False, True]
    assert requests[-1]['state'] == 'closed'
    assert 'say' in [name for name, _ in runner.calls]


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


def _wait_call(tree, link: ScriptedLink, count: int) -> dict:
    for _ in range(400):
        tree.tick()

        if len(link.calls) >= count:
            return link.calls[-1]

        time.sleep(0.005)

    raise AssertionError('no call')


def test_press_judge_sends_the_press_frames_and_pressed_goes_on(tmp_path):
    backend = _push(tmp_path)
    link    = backend.link
    agent   = Agent(backend, press_ctx(PRESS_EVIDENCE))
    runner  = FakeRunner([(True, 'Controller reached target', [])])
    tree    = _tree(JUDGE_TREE, runner, agent)

    call = _wait_call(tree, link, 1)

    assert call['skill'] == 'press_judge'
    assert call['decisions'] == ['pressed', 'not_pressed']
    assert call['press']['frames']['press'].endswith('_press.jpg')

    link.answer({'decision': 'pressed', 'reason': 'the fingertip covers the button'})

    assert _run(tree) == Status.SUCCESS
    assert [call['skill'] for call in link.calls] == ['press_judge']


def test_not_pressed_is_the_failed_check_of_the_press_for_the_recovery(tmp_path):
    backend = _push(tmp_path)
    link    = backend.link
    agent   = Agent(backend, press_ctx(PRESS_EVIDENCE))
    runner  = FakeRunner([(True, 'Controller reached target', [{'t': 0.5, 'distance_to_goal': 0.01}])])
    tree    = _tree(JUDGE_TREE, runner, agent)

    _wait_call(tree, link, 1)
    link.answer({'decision': 'not_pressed', 'reason': 'the finger sits beside the button'})

    recovery = _wait_call(tree, link, 2)

    assert recovery['skill'] == 'manipulation_recovery'
    assert recovery['failure']['skill'] == 'press_judge' and recovery['failure']['checks'] == 'press_button'
    assert recovery['failure']['checked_call']['skill'] == 'press_button'
    assert recovery['failure']['checked_call']['feedback']
    assert {'elapsed_s', 'status', 'message'} <= set(recovery['failure'])      # the executor's trace formats these
    assert recovery['press'] == PRESS_EVIDENCE                                 # the depth and residual of the judged press

    link.answer({'decision': 'ask_help', 'reason': 'r'})
    assert _run(tree) == Status.FAILURE


# ========== offset_retry ==========

def test_offset_retry_needs_the_whole_offset_within_50_mm():
    session = _session('diagnose', {}, {})

    assert session.decide({'decision': 'offset_retry', 'reason': 'r', 'ox': 0.005}).is_error
    assert session.decide({'decision': 'offset_retry', 'reason': 'r', 'ox': 0.06, 'oy': 0.0, 'oz': 0.0}).is_error
    assert not session.decide({'decision': 'offset_retry', 'reason': 'rim', 'ox': 0.005, 'oy': 0.0075, 'oz': 0.0034}).is_error

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
    backend = _push(tmp_path)
    link    = backend.link
    agent   = Agent(backend, press_ctx(PRESS_EVIDENCE))
    runner  = FakeRunner([(True, 'Controller reached target', [{'t': 0.5}]), (True, 'Controller reached target', [])])
    tree    = _tree(OFFSET_TREE, runner, agent)

    _wait_call(tree, link, 1)
    link.answer({'decision': 'not_pressed', 'reason': 'on the rim'})
    recovery = _wait_call(tree, link, 2)
    assert 'offset_retry' in recovery['decisions']
    link.answer({'decision': 'offset_retry', 'reason': 'rim', 'ox': 0.005, 'oy': 0.0075, 'oz': 0.0034})
    _wait_call(tree, link, 3)
    link.answer({'decision': 'pressed', 'reason': 'lit'})

    assert _run(tree) == Status.SUCCESS
    offsets = [inputs for name, inputs in runner.calls if name == 'set_press_offset']
    assert offsets == [{'x': 0.005, 'y': 0.0075, 'z': 0.0034}]
    assert [name for name, _ in runner.calls].count('press_button') == 2


def test_the_bridge_gets_a_long_failure_whole(tmp_path):
    """A 60 s press keeps 120 feedback samples, over the 40 kB a model's tool output is cut to"""
    backend  = _push(tmp_path)
    goal     = {'header': {'frame_id': 'foot_world'}, 'pose': {'position': {'x': 0.4, 'y': -0.1, 'z': 1.0}}}
    feedback = [{'t': 0.5 * index, 'distance_to_goal': float('inf'), 'tracking_lost': False, 'current_goal': goal,
                 'padding': 'x' * 300} for index in range(120)]
    runner   = FakeRunner([(False, 'timeout after 60 s', feedback)])
    tree     = _tree(AGENTIC, runner, Agent(backend, press_ctx(None)))

    call = _wait_call(tree, backend.link, 1)

    assert call['failure'] is not None and len(call['failure']['feedback']) == 120
    backend.link.answer({'decision': 'ask_help', 'reason': 'r'})
    _run(tree)


def test_a_literal_pose_can_carry_the_storey():
    from orchestrator.tree.leaf import parse_pose

    assert parse_pose('3.74 -0.422 2.67') == {'x': 3.74, 'y': -0.422, 'yaw': 2.67, 'frame': 'map'}
    assert parse_pose('3.74, -0.422, -0.435, 2.67')['z'] == -0.435


# ========== stance_check: before the first press ==========

FULL_TREE = TREES / 'freight_elevator_down_floor1_agentic.xml'
PLACES    = {name: {'x': 1.0, 'y': 2.0, 'z': -0.46, 'yaw': 0.0, 'frame': 'map'}
             for name in ('freight_elevator_call_panel_preapproach', 'freight_elevator_call_panel', 'inside_freight_elevator_button_panel')}


def _full_tree(runner: FakeRunner, agent) -> BehaviorTree:
    now     = [0.0]
    clock   = lambda: now.__setitem__(0, now[0] + 0.5) or now[0]          # the tree's Sleeps pass in a few ticks
    context = BTContext(blackboard={name: dict(pose) for name, pose in PLACES.items()}, clock=clock)
    runner.agent = agent

    return BehaviorTree(FULL_TREE.read_text(), make_leaf_factory(Registry.from_directory(CONTRACTS), runner), context)


def test_stance_check_decisions():
    session = _session('stance_check', {'base_goal': PANEL}, {})

    assert session.decide({'decision': 'offset_retry', 'reason': 'x', 'ox': 0, 'oy': 0, 'oz': 0}).is_error
    assert session.decide({'decision': 'ask_help', 'reason': 'x'}).is_error
    assert session.decide({'decision': 'switch_arm', 'reason': 'x', 'use_left_arm': True}).is_error   # the arm is chosen by rule
    assert session.decide({'decision': 'reposition', 'reason': 'too close', 'dx': -0.25, 'dy': 0.0}).final


def test_a_diagnosis_cannot_abort_the_task():
    session = _session('diagnose', {'base_goal': PANEL}, {})

    assert session.decide({'decision': 'abort', 'reason': 'x'}).is_error
    assert session.decide({'decision': 'ask_help', 'reason': 'x'}).final


def test_a_failed_stance_check_is_recorded_for_the_agent():
    runner = FakeRunner([], failing=('button_in_view',))
    body   = ('<root BTCPP_format="4"><BehaviorTree ID="t"><Sequence><button_in_view query="down button"/>'
              '</Sequence></BehaviorTree></root>')
    tree   = _tree(body, runner, None)

    assert _run(tree) == Status.FAILURE
    assert tree.context.last_failure['skill'] == 'button_in_view'
    assert tree.context.last_failure['inputs'] == {'query': 'down button'}


def test_a_poor_stance_is_fixed_before_the_first_press(tmp_path):
    """The view check fails at both panels: outside the agent repositions before pressing, inside it presses as is"""
    backend = _push(tmp_path, [
        {'decision': 'reposition', 'reason': 'stopped 17 cm past', 'dx': -0.3, 'dy': 0.0},
        {'decision': 'pressed', 'reason': 'lit'},
        {'decision': 'continue', 'reason': 'IK reaches both poses'},
        {'decision': 'pressed', 'reason': 'lit'},
    ])
    runner  = FakeRunner([(True, 'Controller reached target', []), (True, 'Controller reached target', [])], failing=('button_in_view',))
    tree    = _full_tree(runner, Agent(backend, press_ctx(PRESS_EVIDENCE)))

    assert _run(tree, max_ticks=3000) == Status.SUCCESS

    names   = [name for name, _ in runner.calls]
    walks   = [inputs['goal'] for name, inputs in runner.calls if name == 'navigate_to']
    phases  = [record['phase'] for record in tree.context.history if 'decision' in record]

    assert phases == ['stance_check', 'judge', 'stance_check', 'judge']
    assert walks[2]['x'] == pytest.approx(0.7) and walks[2]['z'] == pytest.approx(-0.46)   # 0.3 m back, same storey
    assert names.index('navigate_to', names.index('button_in_view')) < names.index('press_button')
    assert names.count('press_button') == 2
    assert backend.link.calls[0]['failure']['skill'] == 'button_in_view'               # the bridge got the failed check


def test_without_an_agent_a_failed_stance_check_still_presses():
    pressed = (True, 'Controller reached target', [])
    runner  = FakeRunner([pressed, pressed], failing=('button_in_view',))
    tree    = _full_tree(runner, None)

    assert _run(tree, max_ticks=3000) == Status.SUCCESS                     # each press_judge fails: a person is asked
    assert [name for name, _ in runner.calls].count('press_button') == 2


def test_confirm_phase_relays_the_question():
    spec     = protocol.load_spec('operator_confirm')
    contract = Registry.from_directory(CONTRACTS).get('operator_confirm')
    call     = AgentCall(contract, {'question': 'Is the gantry removed?'}, BTContext(), 'confirm')

    assert spec.PHASES['confirm'] == ('confirmed', 'not_confirmed') and spec.ANSWERED_BY == 'operator'
    assert 'confirmed' in spec.GOING_ON and 'not_confirmed' not in spec.GOING_ON
    assert 'Is the gantry removed?' in task_text(Agent(None, press_ctx(None)).skill(contract), call)


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
            return FakeHandle(Status.SUCCESS if decision['decision'] in RECOVERY_GOING_ON else Status.FAILURE, decision['decision'],
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
    context = BTContext(blackboard=places, clock=clock)

    runner.agent = type('FakeAgent', (), {'start': lambda self, contract, inputs, context, path='': runner.start(contract, inputs)})()

    return BehaviorTree(load_tree(LIBRARY_RIDE), make_leaf_factory(Registry.from_directory(CONTRACTS), runner), context)


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
    assert [(o['x'], o['y'], o['z']) for o in offsets] == [(0.0, 0.0, 0.0)] * 2 + [(0.005, 0.003, 0.003)] * 2  # each panel's base offset before its looks, then before its first press
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


def _call_panel_calls(runner: 'LibraryRideRunner') -> list[str]:
    """The calls up to the robot asking for the call button to be pressed for it"""
    names = runner.names()
    asked = next(index for index, (name, inputs) in enumerate(runner.calls)
                 if name == 'say' and inputs['text'] == 'Please press the call button for me.')
    return names[:asked]


def test_four_failed_presses_get_three_diagnoses_and_the_fourth_none():
    retry  = {'decision': 'retry', 'left_arm': False}
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 4, judges=[False] * 4, decisions=[retry] * 4)

    _run(_library_ride(runner), max_ticks=4000)
    names = _call_panel_calls(runner)
    assert names.count('press_button') == 4 and names.count('manipulation_recovery') == 3
    last  = len(names) - 1 - names[::-1].index('press_button')
    assert 'manipulation_recovery' not in names[last:]                                # none after the fourth press


def test_a_diagnosis_that_asks_for_help_stops_the_presses():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 4, judges=[False] * 4,
                               decisions=[{'decision': 'ask_help', 'left_arm': False}])

    _run(_library_ride(runner), max_ticks=4000)
    names = _call_panel_calls(runner)
    assert names.count('press_button') == 1 and names.count('manipulation_recovery') == 1


def test_an_offset_retry_presses_again_with_the_offset_the_agent_gave():
    offset = {'decision': 'offset_retry', 'left_arm': False, 'offset_x': 0.012, 'offset_y': 0.0126, 'offset_z': 0.0032}
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 3, judges=[False, True, True], decisions=[offset])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    offsets = [(o['x'], o['y'], o['z']) for name, o in runner.calls if name == 'set_press_offset']
    assert offsets == [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.012, 0.0126, 0.0032), (0.005, 0.003, 0.003), (0.005, 0.003, 0.003)]   # call panel looks, its press, its retry; floor panel looks, its press
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


def test_a_reposition_whose_three_looks_fail_asks_for_help():
    goal   = {'decision': 'reposition', 'left_arm': False, 'goal': {'x': 0.7, 'y': 2.0, 'z': -0.46, 'yaw': 0.0}}
    runner = LibraryRideRunner([(True, 'Controller reached target', [])], judges=[False], decisions=[goal], stance=[True, False, False, False])

    _run(_library_ride(runner), max_ticks=20000)
    names = runner.names()
    says  = [inputs['text'] for name, inputs in runner.calls if name == 'say']
    assert names.count('button_in_view') == 4                           # the stance check, then three looks after the reposition
    assert names.count('press_button') == 1                             # no press after the button is lost
    lost  = says.index('I lost the button after moving.')
    assert says[lost + 1] == 'Please press the call button for me.'     # then the slot asks, as after ask_help
    assert names.count('manipulation_recovery') == 1


def test_a_failed_stance_check_keeps_the_arm_the_rule_chose():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2, stance=[False, True, False, True],
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


def test_the_stance_loop_gives_up_after_nine_repositions_and_asks_for_help():
    goal   = {'decision': 'reposition', 'left_arm': False, 'goal': {'x': 0.7, 'y': 2.0, 'z': -0.46, 'yaw': 0.0}}
    runner = LibraryRideRunner([(True, 'Controller reached target', [])], stance=[False] * 10, decisions=[goal] * 10)
    tree   = _library_ride(runner)

    _run(tree, max_ticks=100000)
    names  = runner.names()
    stance = [inputs for name, inputs in runner.calls if name == 'manipulation_recovery' and inputs['phase'] == 'stance_check']
    says   = [inputs['text'] for name, inputs in runner.calls if name == 'say']
    ask    = next(index for index, (name, inputs) in enumerate(runner.calls) if name == 'say' and inputs['text'].startswith('Please press'))
    assert len(stance) == 10                                            # one after each of the ten looks
    assert names[:ask].count('navigate_to') == 11                       # panel, offset 0, 9 repositions
    assert 'Please press the call button for me.' in says
    assert names.index('release_arms') < ask
    assert 'press_button' not in names[:names.index('release_arms')]    # no blind press at the call panel


class FirstLookWalkFails(LibraryRideRunner):
    def _other(self, name, contract, inputs):
        if name == 'navigate_to' and self.names().count('navigate_to') == 1:     # the walk to the call panel, then the look's
            self.calls.append((name, inputs))
            return FakeHandle(Status.FAILURE, 'blocked')

        return super()._other(name, contract, inputs)


def test_continue_after_a_failed_walk_resets_the_ekf_and_checks_before_the_press():
    runner = FirstLookWalkFails([(True, 'Controller reached target', [])] * 2)

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    names = runner.names()
    first = names[:names.index('press_button')]
    agent = first.index('manipulation_recovery')
    assert 'reset_ekf' not in first[:agent]                                              # the look's walk failed before any reset
    assert first[agent:].count('reset_ekf') == 1
    assert first.index('reset_ekf', agent) < first.index('button_in_view', agent) < first.index('check_reach', agent)


def test_continue_after_a_failed_check_looks_again_without_a_second_reset():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2, stance=[False, False, True])

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    first = runner.names()[:runner.names().index('press_button')]
    agent = first.index('manipulation_recovery')
    walk  = first.index('navigate_to', agent)
    assert first[agent:walk].count('reset_ekf') == 0 and 'button_in_view' in first[agent:walk]   # continue looks here, reset kept
    assert first[walk:].count('reset_ekf') == 1                                                     # the next look walks: a new reset
    assert first.count('button_in_view') == 3 and 'check_reach' in first[walk:]


def test_a_door_that_opens_during_the_judge_halts_the_press_and_walks_in():
    runner = LibraryRideRunner([(True, 'Controller reached target', [])] * 2, hold_judge=True)

    assert _run(_library_ride(runner), max_ticks=20000) == Status.SUCCESS
    names = runner.names()
    assert names.count('press_judge') == 2                                                 # the halted one, then floor 1
    assert 'manipulation_recovery' not in names
    assert names.index('release_arms') > names.index('press_judge')


def test_the_stance_rule_sends_the_base_goal_again_when_the_robot_stands_off_it():
    far    = {'stance_from_base_goal': {'forward_m': -0.286, 'left_m': 0.22, 'yaw_rad': -0.421}}
    turned = {'stance_from_base_goal': {'forward_m': -0.02, 'left_m': 0.01, 'yaw_rad': 0.2}}
    near   = {'stance_from_base_goal': {'forward_m': -0.04, 'left_m': 0.03, 'yaw_rad': -0.1}}

    rule = rule_stance(far, [])
    assert rule['decision'] == 'reposition' and (rule['dx'], rule['dy'], rule['dyaw']) == (0.0, 0.0, 0.0)
    assert rule_stance(turned, [])['decision'] == 'reposition'
    assert rule_stance(near, []) is None
    assert rule_stance({'stance_from_base_goal': None}, []) is None


def test_the_stance_rule_gives_way_to_the_agent_after_two_resends_until_a_press_works():
    far     = {'stance_from_base_goal': {'forward_m': -0.3, 'left_m': 0.0, 'yaw_rad': 0.0}}
    resent  = {'skill': 'manipulation_recovery', 'rule': True, 'rule_name': rule_stance(far, [])['rule_name']}
    pressed = {'skill': 'press_judge', 'decision': 'pressed'}

    assert rule_stance(far, [resent]) is not None
    assert rule_stance(far, [resent, resent]) is None
    assert rule_stance(far, [resent, resent, pressed]) is not None


def test_a_reposition_into_the_panel_is_refused():
    session = _session('diagnose', {'base_goal': PANEL}, {})

    assert session.decide({'decision': 'reposition', 'reason': 'r', 'dx': 0.39, 'dy': 0.1}).is_error
    assert session.decide({'decision': 'reposition', 'reason': 'r', 'dx': 0.0, 'dy': 0.3}).is_error
    assert session.decide({'decision': 'reposition', 'reason': 'r', 'dx': -0.7, 'dy': 0.0}).is_error
    assert session.decide({'decision': 'reposition', 'reason': 'r', 'dx': 0.1, 'dy': -0.05}).final


REACH_MISS = {'skill': 'check_reach', 'status': 'failure', 'outputs': {}}
REACH_MOVE = {'reach': {'reachable': False, 'pelvis_error_m': {'x': -0.08, 'y': 0.01, 'z': 0.0}},
              'reach_move': {'fixable': True, 'within_limits': True, 'dx': 0.1, 'dy': -0.01, 'dyaw': 0.0, 'step_pelvis_m': [0.1, -0.01]}}


def test_the_reach_rule_walks_by_the_ik_miss_after_a_failed_check():
    rule = rule_reach(REACH_MOVE, [], REACH_MISS)

    assert rule['decision'] == 'reposition' and (rule['dx'], rule['dy'], rule['dyaw']) == (0.1, -0.01, 0.0)
    assert rule_reach(REACH_MOVE, [], {'skill': 'button_in_view'}) is None
    assert rule_reach({**REACH_MOVE, 'reach_move': {**REACH_MOVE['reach_move'], 'within_limits': False}}, [], REACH_MISS) is None
    assert rule_reach({**REACH_MOVE, 'reach_move': {'fixable': False}}, [], REACH_MISS) is None


def test_the_reach_rule_gives_way_to_the_agent_after_two_moves():
    moved = {'skill': 'manipulation_recovery', 'rule': True, 'rule_name': rule_reach(REACH_MOVE, [], REACH_MISS)['rule_name']}

    assert rule_reach(REACH_MOVE, [moved], REACH_MISS) is not None
    assert rule_reach(REACH_MOVE, [moved, moved], REACH_MISS) is None


def test_a_diagnosis_may_not_reposition_from_a_stance_the_reach_check_passed():
    def session(moved):
        reach = {'reachable': True, 'message': 'in reach', 'target_moved_since_check_m': {'xyz': [moved, 0.0, 0.0], 'norm': moved}}
        return _session('diagnose', {'base_goal': PANEL}, {'stance': {'reach': reach}})

    assert session(0.01).decide({'decision': 'reposition', 'reason': 'r', 'dx': 0.1, 'dy': 0.0}).is_error
    assert session(0.01).decide({'decision': 'retry', 'reason': 'r'}).final
    assert session(0.05).decide({'decision': 'reposition', 'reason': 'r', 'dx': 0.1, 'dy': 0.0}).final


def test_the_stance_rule_keeps_the_stance_a_reposition_asked_for():
    backed  = {'skill': 'manipulation_recovery', 'decision': 'reposition', 'offset': {'dx': -0.17, 'dy': 0.0, 'dyaw': 0.0}}
    there   = {'stance_from_base_goal': {'forward_m': -0.15, 'left_m': 0.01, 'yaw_rad': 0.02}}
    drifted = {'stance_from_base_goal': {'forward_m': -0.47, 'left_m': 0.29, 'yaw_rad': -0.37}}

    assert rule_stance(there, [backed]) is None
    rule = rule_stance(drifted, [backed])
    assert rule['decision'] == 'reposition' and (rule['dx'], rule['dy'], rule['dyaw']) == (-0.17, 0.0, 0.0)


def test_after_a_continue_the_stance_rule_targets_the_base_goal_as_the_tree_does():
    backed   = {'skill': 'manipulation_recovery', 'decision': 'reposition', 'offset': {'dx': -0.17, 'dy': 0.0, 'dyaw': 0.0}}
    kept     = {'skill': 'manipulation_recovery', 'decision': 'continue', 'offset': None}
    at_base  = {'stance_from_base_goal': {'forward_m': 0.02, 'left_m': -0.01, 'yaw_rad': 0.01}}
    backed_2 = {'skill': 'manipulation_recovery', 'decision': 'reposition', 'offset': {'dx': -0.12, 'dy': 0.05, 'dyaw': 0.0}}
    there_2  = {'stance_from_base_goal': {'forward_m': -0.12, 'left_m': 0.05, 'yaw_rad': 0.0}}

    assert rule_stance(at_base, [backed]) is not None            # before the continue: the reposition is the target
    assert rule_stance(at_base, [backed, kept]) is None           # after it: the base goal, where the tree walked
    assert rule_stance(there_2, [backed, kept, backed_2]) is None # a later reposition is the target again


def test_a_judged_miss_by_millimetres_goes_to_the_model_with_its_aim_crops(tmp_path):
    aim     = {'frames': {'contact_aim': '/tmp/press_1.000_press_aim.jpg'}, 'geometry': {}, 'size_px': [440, 320]}
    press   = {**PRESS_EVIDENCE, 'capture': {**PRESS_EVIDENCE['capture'], 'residual_m': [-0.002, -0.0051, 0.0002]}, 'aim': aim}
    backend = _push(tmp_path)
    link    = backend.link
    tree    = _tree(JUDGE_TREE, FakeRunner([(True, 'Controller reached target', [])]), Agent(backend, press_ctx(press)))

    _wait_call(tree, link, 1)
    link.answer({'decision': 'not_pressed', 'reason': 'the fingertip sits on the rim'})
    recovery = _wait_call(tree, link, 2)

    assert recovery['phase'] == 'diagnose' and recovery['press']['aim'] == aim

    link.answer({'decision': 'ask_help', 'reason': 'r'})
    assert _run(tree) == Status.FAILURE


MISSED  = {'skill': 'press_judge', 'status': 'failure', 'outputs': {'decision': 'not_pressed'}}
SOUND   = {'reach': {'reachable': True, 'message': 'in reach', 'target_moved_since_check_m': {'xyz': [0.004, 0.0, 0.0], 'norm': 0.004}},
           'stance_from_base_goal': {'forward_m': -0.186, 'left_m': 0.017, 'yaw_rad': -0.056}}    # run 006: 0.187 m off


def test_a_press_missed_from_a_sound_stance_is_pressed_again_from_there_once():
    rule = rule_repress(SOUND, [], MISSED)
    assert rule['decision'] == 'retry' and rule['rule_name'] == 'repress'

    repressed = {'skill': 'manipulation_recovery', 'rule': True, 'rule_name': 'repress'}
    pressed   = {'skill': 'press_judge', 'decision': 'pressed'}
    assert rule_repress(SOUND, [repressed], MISSED) is None
    assert rule_repress(SOUND, [repressed, pressed], MISSED) is not None


def test_the_repress_rule_needs_a_judged_miss_from_a_passed_reach_check_and_an_unmoved_target():
    moved   = {**SOUND, 'reach': {**SOUND['reach'], 'target_moved_since_check_m': {'xyz': [0.05, 0.0, 0.0], 'norm': 0.05}}}
    failed  = {**SOUND, 'reach': {**SOUND['reach'], 'reachable': False}}
    timeout = {'skill': 'press_button', 'status': 'failure', 'outputs': {}}

    assert rule_repress(moved, [], MISSED) is None
    assert rule_repress(failed, [], MISSED) is None
    assert rule_repress(SOUND, [], timeout) is None
    assert rule_repress({'error': 'no transform'}, [], MISSED) is None


def test_a_diagnosis_after_a_miss_from_a_sound_stance_never_walks_by_the_stance_rule():
    skill = Agent(None, press_ctx(None)).skill(Registry.from_directory(CONTRACTS).get('manipulation_recovery'))
    call  = AgentCall(Registry.from_directory(CONTRACTS).get('manipulation_recovery'), {'base_goal': PANEL}, BTContext(), 'diagnose')

    found, speech = skill.rule(call, {'stance': SOUND, 'history': [], 'last_failure': MISSED})
    assert found['decision'] == 'retry' and speech == RULE_SPEECH['repress']

    repressed = {'skill': 'manipulation_recovery', 'rule': True, 'rule_name': 'repress'}
    assert skill.rule(call, {'stance': SOUND, 'history': [repressed], 'last_failure': MISSED}) is None   # the model decides

    timeout = {'skill': 'press_button', 'status': 'failure', 'outputs': {}}
    found, _ = skill.rule(call, {'stance': SOUND, 'history': [], 'last_failure': timeout})
    assert found['rule_name'] == 'stance'                                     # not a judged miss: the stance rule as before
