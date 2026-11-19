"""TreeRunner runs one tree at a time and traces it; which trees may run is not its decision."""

import pytest

from skill_registry import Registry

from orchestrator.tree.engine import Status, TreeError
from orchestrator.tree.leaf import make_leaf_factory
from orchestrator.tree.runner import TreeRunner

from test_behavior_tree import CONTRACTS, FakeRunner


class FakeTrace:
    def __init__(self):
        self.fields = {}
        self.events = []

    def emit(self, kind: str, text: str, level: str = 'info', **data):
        self.events.append({'kind': kind, 'text': text, 'level': level, **self.fields, **data})

    def kinds(self) -> list[str]:
        return [event['kind'] for event in self.events if event['kind'] != 'node']


class FakeLogger:
    def __init__(self):
        self.errors = []

    def error(self, message: str):
        self.errors.append(message)


class CrashingRunner(FakeRunner):
    def start(self, contract, inputs):
        raise RuntimeError('runner bug')


def _xml(tree_id: str, body: str = '<say text="hello"/>') -> str:
    return f'<root BTCPP_format="4"><BehaviorTree ID="{tree_id}">{body}</BehaviorTree></root>'


def _runner(skill_runner=None, **options) -> tuple[TreeRunner, FakeTrace, list]:
    trace, statuses = FakeTrace(), []
    factory = make_leaf_factory(Registry.from_directory(CONTRACTS), skill_runner or FakeRunner())
    runner  = TreeRunner(factory, trace, lambda state, tree_id: statuses.append((state, tree_id)), FakeLogger(), **options)

    return runner, trace, statuses


def _start(runner: TreeRunner, tree_id: str, body: str = '<say text="hello"/>'):
    runner.start(runner.build(_xml(tree_id, body)))


def _finish(runner: TreeRunner, max_ticks: int = 20):
    for _ in range(max_ticks):
        if runner.tree is None:
            return

        runner.tick()


def test_a_tree_runs_to_its_end_and_reports_it():
    finished = []
    runner, trace, statuses = _runner(on_finished=lambda tree_id, status: finished.append((tree_id, status)))

    _start(runner, 'task')
    _finish(runner)

    assert finished == [('task', Status.SUCCESS)]
    assert statuses == [('running', 'task'), ('success', 'task')]      # the final status still names the tree
    assert trace.kinds() == ['started', 'record', 'finished'] and trace.fields == {}


def test_every_event_of_a_run_carries_its_run_id():
    runner, trace, _ = _runner()

    _start(runner, 'task')
    run_id = runner.tree.context.run_id
    _finish(runner)

    assert run_id and all(event['run_id'] == run_id for event in trace.events)


def test_the_blackboard_is_copied_into_each_tree():
    places = {'call_panel': {'x': 1.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}
    runner, _, _ = _runner(blackboard=places)

    tree = runner.build(_xml('task'))
    tree.context.blackboard['call_panel'] = None

    assert places['call_panel'] is not None


def test_a_tree_that_does_not_build_raises():
    runner, _, _ = _runner()

    with pytest.raises((TreeError, ValueError)):
        runner.build('<root BTCPP_format="4"><BehaviorTree ID="t"><no_such_skill/></BehaviorTree></root>')


def test_a_new_tree_halts_the_running_one():
    runner, trace, _ = _runner(FakeRunner({'say': [Status.RUNNING]}))

    _start(runner, 'first')
    _start(runner, 'second')

    assert runner.tree.id == 'second'
    assert trace.kinds() == ['started', 'halted', 'started']


def test_cancel_halts_the_tree_and_clears_the_run_fields():
    finished = []
    runner, trace, statuses = _runner(FakeRunner({'say': [Status.RUNNING]}),
                                      on_finished=lambda tree_id, status: finished.append(tree_id))

    _start(runner, 'task')
    runner.cancel()

    assert runner.tree is None and trace.fields == {}
    assert statuses[-1] == ('cancelled', None) and finished == []


def test_a_crash_in_a_tick_stops_the_tree_but_not_the_runner():
    runner, trace, statuses = _runner(CrashingRunner())

    _start(runner, 'task')
    runner.tick()

    assert runner.tree is None and statuses[-1] == ('error', 'task')
    assert runner.logger.errors and trace.kinds()[-1] == 'error'


def test_the_started_event_takes_any_field_even_tree():
    runner, trace, _ = _runner()

    runner.start(runner.build(_xml('task')), ' (blind)', tree='<root/>', blind=True)

    assert trace.events[0]['text'] == 'running task (blind)' and trace.events[0]['tree'] == '<root/>'
