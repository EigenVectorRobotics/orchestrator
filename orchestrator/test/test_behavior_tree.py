from pathlib import Path

import pytest

from skill_registry import Registry

from orchestrator.behavior_tree import BehaviorTree, Context, Status, TreeError
from orchestrator.skill_leaf import make_leaf_factory


CONTRACTS = Path(__file__).resolve().parents[2] / 'skill_registry' / 'contracts'
EXAMPLE   = '''<root BTCPP_format="4">
  <BehaviorTree ID="call_elevator">
    <Sequence>
      <navigate_to goal="{call_panel}"/>
      <Fallback>
        <press_button target_description="up button"/>
        <Sequence>
          <navigate_to goal="{call_panel}"/>
          <Fallback>
            <press_button target_description="up button"/>
            <say text="Please press the up button for me."/>
          </Fallback>
        </Sequence>
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
</root>'''


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeHandle:
    def __init__(self, script: list[Status], outputs: dict):
        self.script    = list(script)
        self.outputs   = outputs
        self.message   = ''
        self.cancelled = False

    def poll(self) -> Status:
        if self.cancelled:
            return Status.FAILURE

        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def cancel(self):
        self.cancelled = True


class FakeRunner:
    """Each skill plays a script of statuses, one per tick; default: success at once"""

    def __init__(self, scripts: dict[str, list] | None = None, outputs: dict | None = None):
        self.scripts = scripts or {}
        self.outputs = outputs or {}
        self.calls   = []
        self.handles = []

    def start(self, contract, inputs):
        self.calls.append((contract.name, inputs))

        script = self.scripts.get(contract.name, [Status.SUCCESS])

        if script and isinstance(script[0], list):     # one script per call
            script = script.pop(0) if len(script) > 1 else script[0]

        handle = FakeHandle(script, self.outputs.get(contract.name, {}))
        self.handles.append(handle)

        return handle


def _tree(body: str, runner: FakeRunner, clock: FakeClock | None = None, blackboard: dict | None = None) -> BehaviorTree:
    xml_text = f'<root BTCPP_format="4"><BehaviorTree ID="main">{body}</BehaviorTree></root>'
    context  = Context(blackboard=blackboard, clock=clock or FakeClock())

    return BehaviorTree(xml_text, make_leaf_factory(Registry.from_directory(CONTRACTS), runner), context)


def _run(tree: BehaviorTree, clock: FakeClock | None = None, max_ticks: int = 100) -> Status:
    for _ in range(max_ticks):
        status = tree.tick()

        if status != Status.RUNNING:
            return status

        if clock:
            clock.now += 0.1

    return Status.RUNNING


def test_sequence_runs_in_order_and_parses_inputs():
    runner = FakeRunner()
    tree   = _tree('<Sequence><say text="hi"/><navigate_to goal="1 2 0.5"/></Sequence>', runner)

    assert _run(tree) == Status.SUCCESS
    assert runner.calls == [
        ('say', {'text': 'hi'}),
        ('navigate_to', {'goal': {'x': 1.0, 'y': 2.0, 'yaw': 0.5, 'frame': 'map'}}),
    ]


def test_running_child_is_resumed_not_restarted():
    runner = FakeRunner({'navigate_to': [Status.RUNNING, Status.RUNNING, Status.SUCCESS]})
    tree   = _tree('<Sequence><say text="a"/><navigate_to goal="0 0 0"/></Sequence>', runner)

    assert _run(tree) == Status.SUCCESS
    assert [name for name, _ in runner.calls] == ['say', 'navigate_to']


def test_fallback_uses_recovery_after_failure():
    runner = FakeRunner({'press_button': [Status.FAILURE]})
    tree   = _tree('<Fallback><press_button target_description="up"/><say text="help"/></Fallback>', runner)

    assert _run(tree) == Status.SUCCESS
    assert [name for name, _ in runner.calls] == ['press_button', 'say']


def test_retry_runs_again():
    runner = FakeRunner({'door_open': [[Status.FAILURE], [Status.FAILURE], [Status.SUCCESS]]})
    tree   = _tree('<RetryUntilSuccessful num_attempts="5"><door_open/></RetryUntilSuccessful>', runner)

    assert _run(tree) == Status.SUCCESS
    assert len(runner.calls) == 3


def test_retry_gives_up():
    runner = FakeRunner({'door_open': [Status.FAILURE]})
    tree   = _tree('<RetryUntilSuccessful num_attempts="3"><door_open/></RetryUntilSuccessful>', runner)

    assert _run(tree) == Status.FAILURE
    assert len(runner.calls) == 3


def test_contract_timeout_cancels_skill():
    clock  = FakeClock()
    runner = FakeRunner({'say': [Status.RUNNING]})               # say has timeout_s 10
    tree   = _tree('<say text="long"/>', runner, clock)

    assert _run(tree, clock, max_ticks=200) == Status.FAILURE
    assert runner.handles[0].cancelled
    assert tree.root.message == 'timeout after 10 s'


def test_timeout_decorator_halts_child():
    clock  = FakeClock()
    runner = FakeRunner({'navigate_to': [Status.RUNNING]})
    tree   = _tree('<Timeout msec="500"><navigate_to goal="0 0 0"/></Timeout>', runner, clock)

    assert _run(tree, clock) == Status.FAILURE
    assert runner.handles[0].cancelled


def test_reactive_sequence_halts_running_action_when_condition_fails():
    runner = FakeRunner({
        'localized':   [[Status.SUCCESS], [Status.FAILURE]],
        'navigate_to': [Status.RUNNING],
    })
    tree   = _tree('<ReactiveSequence><localized/><navigate_to goal="0 0 0"/></ReactiveSequence>', runner)

    assert tree.tick() == Status.RUNNING
    assert tree.tick() == Status.FAILURE
    assert runner.handles[1].cancelled


def test_parallel_needs_all_by_default():
    runner = FakeRunner({'navigate_to': [Status.RUNNING, Status.SUCCESS]})
    tree   = _tree('<Parallel><navigate_to goal="0 0 0"/><say text="walking"/></Parallel>', runner)

    assert tree.tick() == Status.RUNNING
    assert tree.tick() == Status.SUCCESS
    assert len(runner.calls) == 2                              # say is not run again


def test_blackboard_inputs_and_outputs():
    runner     = FakeRunner(outputs={'ask_vlm': {'answer': 'two'}})
    blackboard = {'panel': {'x': 3.0, 'y': 4.0, 'yaw': 0.0, 'frame': 'map'}}
    tree       = _tree(
        '<Sequence><navigate_to goal="{panel}"/>'
        '<ask_vlm question="Which floor?" choices="one;two" answer="{floor}"/></Sequence>',
        runner,
        blackboard=blackboard
    )

    assert _run(tree) == Status.SUCCESS
    assert runner.calls[0][1]['goal']['x'] == 3.0
    assert blackboard['floor'] == 'two'


def test_missing_blackboard_entry_fails_the_skill():
    tree = _tree('<navigate_to goal="{nowhere}"/>', FakeRunner())

    assert tree.tick() == Status.FAILURE
    assert 'blackboard has no nowhere' in tree.root.message


def test_subtree_and_events():
    events   = []
    xml_text = (
        '<root BTCPP_format="4">'
        '<BehaviorTree ID="main"><Sequence><SubTree ID="greet"/><AlwaysSuccess/></Sequence></BehaviorTree>'
        '<BehaviorTree ID="greet"><say text="hello"/></BehaviorTree>'
        '</root>'
    )
    tree = BehaviorTree(
        xml_text,
        make_leaf_factory(Registry.from_directory(CONTRACTS), FakeRunner()),
        Context(on_event=lambda node, event: events.append(event), clock=FakeClock())
    )

    assert _run(tree) == Status.SUCCESS
    assert ('main/Sequence[0]/SubTree/say', 'success') in [(event['path'], event['status']) for event in events]


def test_unknown_skill_fails_at_build():
    with pytest.raises(TreeError):
        _tree('<fly/>', FakeRunner())


def test_example_tree_builds_and_runs():
    clock  = FakeClock()
    runner = FakeRunner({'press_button': [Status.FAILURE]})
    tree   = BehaviorTree(
        EXAMPLE,
        make_leaf_factory(Registry.from_directory(CONTRACTS), runner),
        Context(blackboard={'call_panel': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}, clock=clock)
    )

    assert _run(tree, clock) == Status.SUCCESS
    assert [name for name, _ in runner.calls] == ['navigate_to', 'press_button', 'navigate_to', 'press_button', 'say', 'door_open']


def test_example_tree_fails_when_door_stays_closed():
    clock  = FakeClock()
    runner = FakeRunner({'door_open': [Status.FAILURE]})
    tree   = BehaviorTree(
        EXAMPLE,
        make_leaf_factory(Registry.from_directory(CONTRACTS), runner),
        Context(blackboard={'call_panel': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}, clock=clock)
    )

    assert _run(tree, clock, max_ticks=2000) == Status.FAILURE
    assert sum(name == 'door_open' for name, _ in runner.calls) == 60
    assert runner.calls[-1] == ('say', {'text': 'The elevator door did not open.'})


def test_sequence_with_memory_does_not_rerun_done_children():
    runner = FakeRunner({'press_button': [[Status.FAILURE], [Status.SUCCESS]]})
    tree   = _tree('<SequenceWithMemory><say text="a"/><press_button target_description="up"/></SequenceWithMemory>', runner)

    assert tree.tick() == Status.FAILURE
    assert tree.tick() == Status.SUCCESS
    assert [name for name, _ in runner.calls] == ['say', 'press_button', 'press_button']


def test_reactive_fallback_halts_recovery_when_condition_comes_back():
    runner = FakeRunner({'localized': [[Status.FAILURE], [Status.SUCCESS]], 'say': [Status.RUNNING]})
    tree   = _tree('<ReactiveFallback><localized/><say text="lost"/></ReactiveFallback>', runner)

    assert tree.tick() == Status.RUNNING
    assert tree.tick() == Status.SUCCESS
    assert runner.handles[1].cancelled


def test_force_failure_and_parallel_counts():
    assert _run(_tree('<ForceFailure><say text="x"/></ForceFailure>', FakeRunner())) == Status.FAILURE

    runner = FakeRunner({'navigate_to': [Status.FAILURE]})
    tree   = _tree('<Parallel success_count="1"><navigate_to goal="0 0 0"/><say text="hi"/></Parallel>', runner)

    assert _run(tree) == Status.SUCCESS


def test_example_tree_fails_when_the_walk_fails():
    runner = FakeRunner({'navigate_to': [Status.FAILURE]})
    tree   = BehaviorTree(
        EXAMPLE,
        make_leaf_factory(Registry.from_directory(CONTRACTS), runner),
        Context(blackboard={'call_panel': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}, clock=FakeClock())
    )

    assert _run(tree) == Status.FAILURE
    assert [name for name, _ in runner.calls] == ['navigate_to']


def test_open_door_halts_the_press_and_walks_in():
    runner = FakeRunner({
        'door_open':    [[Status.FAILURE], [Status.FAILURE], [Status.FAILURE], [Status.SUCCESS]],
        'press_button': [Status.RUNNING],
    })
    tree   = _tree(
        '<Sequence>'
        '<ReactiveFallback><door_open/>'
        '<Sequence><navigate_to goal="1 2 0"/><press_button target_description="down button"/></Sequence>'
        '</ReactiveFallback>'
        '<Parallel success_count="2" failure_count="1"><release_arms/><navigate_to goal="3 4 0"/></Parallel>'
        '</Sequence>',
        runner
    )

    assert _run(tree) == Status.SUCCESS
    press = next(handle for (name, _), handle in zip(runner.calls, runner.handles) if name == 'press_button')
    assert press.cancelled
    assert [name for name, _ in runner.calls][-2:] == ['release_arms', 'navigate_to']


def test_an_open_door_on_another_floor_does_not_stop_the_floor_press():
    runner = FakeRunner({
        'inside_cabin': [Status.SUCCESS],
        'door_open':    [Status.SUCCESS],                                   # the door is still open at floor 2
        'at_floor':     [[Status.FAILURE], [Status.FAILURE], [Status.SUCCESS]],
        'press_button': [Status.RUNNING],
    })
    tree   = _tree(
        '<Sequence>'
        '<ReactiveFallback><Sequence><inside_cabin/><at_floor floor="1"/><door_open/></Sequence>'
        '<press_button target_description="floor 1"/></ReactiveFallback>'
        '<navigate_to goal="3 4 0"/>'
        '</Sequence>',
        runner
    )

    assert _run(tree) == Status.SUCCESS
    names = [name for name, _ in runner.calls]
    press = runner.handles[names.index('press_button')]
    assert press.cancelled
    assert names.count('at_floor') == 3 and names[-1] == 'navigate_to'


def test_set_blackboard_copies_a_place_into_a_parameter():
    runner     = FakeRunner()
    place      = {'x': 1.0, 'y': 2.0, 'z': 0.0, 'yaw': 0.0}
    blackboard = {'target_floor_1': place}
    tree       = _tree(
        '<Sequence><SetBlackboard value="{target_floor_1}" output_key="target"/><navigate_to goal="{target}"/></Sequence>',
        runner,
        blackboard=blackboard
    )

    assert _run(tree) == Status.SUCCESS
    assert blackboard['target'] is place
    assert runner.calls[-1][0] == 'navigate_to'
