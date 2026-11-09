import time
import threading

from skill_registry import Registry

from orchestrator.tasks import Tasks
from orchestrator.skill_leaf import make_leaf_factory
from orchestrator.behavior_tree import Context, Status

from test_behavior_tree import CONTRACTS, FakeClock, FakeRunner


PANEL = {'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'frame': 'map'}
TREE  = '<root BTCPP_format="4"><BehaviorTree ID="walk"><navigate_to goal="{call_panel}"/></BehaviorTree></root>'


class FakeTracer:
    def __init__(self):
        self.fields = {}
        self.events = []

    def emit(self, kind, text, level='info', **data):
        self.events.append({'kind': kind, **self.fields, **data})

    def kinds(self):
        return [event['kind'] for event in self.events if event['kind'] != 'validation']


def _tasks(runner: FakeRunner):
    registry = Registry.from_directory(CONTRACTS)
    tracer   = FakeTracer()
    statuses = []
    tasks    = Tasks(registry, make_leaf_factory(registry, runner), tracer, lambda state, tree: statuses.append(state),
                     {'nuc', 'orin'}, make_scratch=lambda run_id: f'/scratch/{run_id}')

    return tasks, tracer, statuses


def _context(run_id='r1'):
    return Context(blackboard={'call_panel': dict(PANEL)}, clock=FakeClock(), run_id=run_id)


def _tick_until(tasks, thread, max_ticks=200):
    for _ in range(max_ticks):
        tasks.tick()

        if not thread.is_alive():
            return

        time.sleep(0.005)


def test_a_tree_sent_alone_is_a_task_of_one_step():
    tasks, tracer, statuses = _tasks(FakeRunner({'navigate_to': [Status.RUNNING, Status.SUCCESS]}))

    assert tasks.run_tree(TREE, _context(), blind=False)

    tasks.tick()
    tasks.tick()

    assert tracer.events[-1]['end'] == 'success' and tracer.events[-1]['steps'] == 1
    assert statuses == ['running', 'success'] and tasks.task is None


def test_a_new_tree_halts_the_task_and_a_cancel_ends_it():
    tasks, tracer, statuses = _tasks(FakeRunner({'navigate_to': [Status.RUNNING]}))

    tasks.run_tree(TREE, _context('r1'), blind=False)
    tasks.run_tree(TREE, _context('r2'), blind=False)
    tasks.cancel()

    ends = [(event['run_id'], event['end']) for event in tracer.events if event['kind'] == 'task_finished']

    assert ends == [('r1', 'halted'), ('r2', 'cancelled')]
    assert tracer.kinds().count('halted') == 2 and tasks.task is None


def test_steps_of_a_task_share_its_blackboard_and_return_their_results():
    runner = FakeRunner({'navigate_to': [Status.RUNNING, Status.SUCCESS]}, outputs={'ground': {'detections': 'boxes'}})
    tasks, tracer, _ = _tasks(runner)
    results          = []

    task = tasks.start_task('call the elevator', _context())

    def agent():
        results.append(tasks.run_skill('ground', {'query': 'up'}))
        results.append(tasks.run_skill('navigate_to', {'goal': {'x': 3.0, 'y': 4.0, 'yaw': 0.0, 'frame': 'map'}}))
        tasks.end_task(task, 'done')

    thread = threading.Thread(target=agent)
    thread.start()
    _tick_until(tasks, thread)
    thread.join()

    assert [result.status for result in results] == ['success', 'success']
    assert results[0].outputs == {'step1_detections': 'boxes'}
    assert runner.calls[1][1]['goal']['x'] == 3.0                               # a dict input went by the blackboard
    assert [event['step'] for event in tracer.events if event['kind'] == 'started'] == [1, 2]
    assert tracer.events[-1]['end'] == 'done' and tracer.events[-1]['steps'] == 2


def test_a_failed_step_returns_its_failure_and_the_task_stays_open():
    tasks, _, _ = _tasks(FakeRunner({'navigate_to': [Status.FAILURE]}))
    results     = []

    tasks.start_task('walk', _context())
    thread = threading.Thread(target=lambda: results.append(tasks.run_skill('navigate_to', {'goal': '{call_panel}'})))
    thread.start()
    _tick_until(tasks, thread)
    thread.join()

    assert results[0].status == 'failure' and results[0].failure['skill'] == 'navigate_to'
    assert tasks.task is not None


def test_run_skill_refuses_without_a_task_and_for_an_unknown_skill_or_input():
    tasks, _, _ = _tasks(FakeRunner())

    assert tasks.run_skill('say', {'text': 'hi'}).status == 'error'                # no task

    tasks.start_task('t', _context())

    assert 'no skill' in tasks.run_skill('fly', {}).message
    assert tasks.run_skill('navigate_to', {'goal': '{lobby}'}).status == 'error'   # not on the blackboard


def test_a_stopped_agent_halts_its_step():
    tasks, tracer, _ = _tasks(FakeRunner({'navigate_to': [Status.RUNNING]}))
    stop, results    = threading.Event(), []

    tasks.start_task('walk', _context())
    thread = threading.Thread(target=lambda: results.append(tasks.run_skill('navigate_to', {'goal': '{call_panel}'}, stop.is_set)))
    thread.start()
    tasks.tick()
    stop.set()
    _tick_until(tasks, thread)
    thread.join()

    assert results[0].status == 'halted' and 'halted' in tracer.kinds()


def test_an_abort_ends_one_step_and_a_replaced_task_is_not_ended_by_its_agent():
    tasks, tracer, _ = _tasks(FakeRunner())
    results          = []
    task             = tasks.start_task('walk', _context('r1'))
    task.context.abort('the agent aborted the last step')

    thread = threading.Thread(target=lambda: results.append(tasks.run_skill('say', {'text': 'hi'})))
    thread.start()
    _tick_until(tasks, thread)
    thread.join()

    assert results[0].status == 'success'                                    # the old abort does not fail it

    tasks.run_tree(TREE, _context('r2'), blind=False)                       # a tree sent alone replaces the task
    tasks.end_task(task, 'done')

    assert tasks.task.run_id == 'r2'
    assert [event['end'] for event in tracer.events if event['kind'] == 'task_finished'] == ['halted']
