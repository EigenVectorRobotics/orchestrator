"""TreeRunner runs one tree at a time behind the recorder and preflight gates, with no ROS."""

from skill_registry import Registry

from orchestrator.execution.tree_runner import PREFLIGHT_TREE, TreeRunner
from orchestrator.tree.engine import Status

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
    runner = TreeRunner(Registry.from_directory(CONTRACTS), {}, {'nuc', 'orin'}, skill_runner or FakeRunner(), trace,
                        lambda state, tree_id: statuses.append((state, tree_id)), FakeLogger(), **options)

    return runner, trace, statuses


def _finish(runner: TreeRunner, max_ticks: int = 20):
    for _ in range(max_ticks):
        if runner.tree is None:
            return

        runner.tick()


def test_a_task_tree_runs_only_after_a_passed_preflight():
    runner, trace, statuses = _runner(require_preflight=True)

    assert not runner.run(_xml('task'))
    assert statuses[-1] == ('rejected', None)

    assert runner.run(_xml(PREFLIGHT_TREE))
    _finish(runner)
    assert runner.preflight_passed and statuses[-1] == ('success', PREFLIGHT_TREE)     # the status names the tree that ended

    assert runner.run(_xml('task'))
    assert statuses[-1] == ('running', 'task')


def test_a_failed_preflight_closes_the_gate_again():
    runner, _, _ = _runner(FakeRunner({'say': [Status.FAILURE]}), require_preflight=True)
    runner.preflight_passed = True

    runner.run(_xml(PREFLIGHT_TREE))
    _finish(runner)

    assert not runner.preflight_passed
    assert not runner.run(_xml('task'))


def test_skip_runs_a_task_tree_without_preflight_and_traces_it():
    runner, trace, _ = _runner(require_preflight=True)

    assert runner.run(_xml('task'), skip=True)
    assert 'preflight_skipped' in trace.kinds()


def test_the_recorder_gate_refuses_with_its_reason():
    runner, trace, statuses = _runner(require_recorder=True, recorder_ready=lambda: (False, 'disk full'))

    assert not runner.run(_xml('task'))
    assert trace.events[-1]['kind'] == 'rejected' and 'disk full' in trace.events[-1]['text']
    assert statuses == [('rejected', None)]


def test_an_invalid_tree_is_rejected_after_its_issues_are_traced():
    runner, trace, _ = _runner()

    assert not runner.run(_xml('task', '<no_such_skill/>'))
    assert trace.kinds()[-2:] == ['validation', 'rejected']


def test_a_new_tree_halts_the_running_one():
    runner, trace, _ = _runner(FakeRunner({'say': [Status.RUNNING]}))

    runner.run(_xml('first'))
    runner.run(_xml('second'))

    assert runner.tree.id == 'second'
    assert any(event['kind'] == 'halted' and 'first' in event['text'] for event in trace.events)


def test_cancel_halts_the_tree_and_clears_the_run_fields():
    runner, trace, statuses = _runner(FakeRunner({'say': [Status.RUNNING]}))

    runner.run(_xml('task'))
    assert trace.fields['tree_id'] == 'task'

    runner.cancel()

    assert runner.tree is None and trace.fields == {}
    assert statuses[-1] == ('cancelled', None)


def test_a_crash_in_a_tick_stops_the_tree_but_not_the_runner():
    runner, trace, statuses = _runner(CrashingRunner())

    runner.run(_xml('task'))
    runner.tick()

    assert runner.tree is None and statuses[-1] == ('error', 'task')
    assert runner.logger.errors and trace.kinds()[-1] == 'error'
