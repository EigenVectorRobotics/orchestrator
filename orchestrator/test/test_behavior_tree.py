import pytest

from skill_registry import Registry

from orchestrator.behavior_tree import BehaviorTree, Context, Status, TreeError
from orchestrator.skill_leaf import make_leaf_factory, parse_pose

from data_paths import SKILLS

from fakes import FakeClock, FakeRunner


def _tree(body: str, runner: FakeRunner, clock: FakeClock | None = None, blackboard: dict | None = None) -> BehaviorTree:
    xml_text = f'<root BTCPP_format="4"><BehaviorTree ID="main">{body}</BehaviorTree></root>'
    context  = Context(blackboard=blackboard, clock=clock or FakeClock())

    return BehaviorTree(xml_text, make_leaf_factory(Registry.from_directory(SKILLS), runner), context)


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


def test_retry_runs_again_and_gives_up():
    runner = FakeRunner({'door_open': [[Status.FAILURE], [Status.FAILURE], [Status.SUCCESS]]})
    tree   = _tree('<RetryUntilSuccessful num_attempts="5"><door_open/></RetryUntilSuccessful>', runner)

    assert _run(tree) == Status.SUCCESS
    assert len(runner.calls) == 3

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
    assert 'timeout' in tree.root.message


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
    assert 'nowhere' in tree.root.message


def test_a_blackboard_value_of_another_type_fails_the_skill_before_it_reaches_ros():
    runner = FakeRunner()                       # 2026-11-20: a place's pose on a string port stopped the executor
    tree   = _tree('<say text="{panel}"/>', runner, blackboard={'panel': {'x': 3.0, 'y': 4.0, 'yaw': 0.0}})

    assert tree.tick() == Status.FAILURE
    assert 'text takes a string' in tree.root.message and runner.calls == []

    tree = _tree('<say text="{floor}"/>', runner, blackboard={'floor': 2})   # a number on a text port goes as text

    assert _run(tree) == Status.SUCCESS and runner.calls[0][1]['text'] == '2'


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
        make_leaf_factory(Registry.from_directory(SKILLS), FakeRunner()),
        Context(on_event=lambda node, event: events.append(event), clock=FakeClock())
    )

    assert _run(tree) == Status.SUCCESS
    assert ('main/Sequence[0]/SubTree/say', 'success') in [(event['path'], event['status']) for event in events]


def test_unknown_skill_fails_at_build():
    with pytest.raises(TreeError):
        _tree('<fly/>', FakeRunner())


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

    runner = FakeRunner({'navigate_to': [Status.RUNNING, Status.SUCCESS]})
    tree   = _tree('<Parallel><navigate_to goal="0 0 0"/><say text="walking"/></Parallel>', runner)

    assert tree.tick() == Status.RUNNING                      # all children by default
    assert tree.tick() == Status.SUCCESS
    assert len(runner.calls) == 2                              # say is not run again

    runner = FakeRunner({'navigate_to': [Status.FAILURE]})
    tree   = _tree('<Parallel success_count="1"><navigate_to goal="0 0 0"/><say text="hi"/></Parallel>', runner)

    assert _run(tree) == Status.SUCCESS


def test_a_literal_pose_can_carry_the_storey():
    assert parse_pose('3.74 -0.422 2.67') == {'x': 3.74, 'y': -0.422, 'yaw': 2.67, 'frame': 'map'}
    assert parse_pose('3.74, -0.422, -0.435, 2.67')['z'] == -0.435
