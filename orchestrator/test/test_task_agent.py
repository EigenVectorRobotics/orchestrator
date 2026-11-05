import threading

from types import SimpleNamespace

from orchestrator.task_agent import LOST_SPEECH, TaskDesk
from orchestrator.behavior_tree import Status

from test_tasks import _context, _tasks, _tick_until
from test_behavior_tree import FakeClock, FakeRunner


def _desk(runner: FakeRunner, tmp_path, **limits):
    tasks, tracer, _ = _tasks(runner)
    opened, replies  = [], []
    said, clock      = [], FakeClock()
    desk             = TaskDesk(tasks, opened.append, replies.append, said.append, clock=clock, **limits)
    context          = _context()
    context.blackboard['call_panel'] = {'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'frame': 'map'}

    tasks.make_scratch = lambda run_id: str(tmp_path)
    desk.open('Go to the call panel', context)

    return SimpleNamespace(desk=desk, tasks=tasks, tracer=tracer, opened=opened, replies=replies, said=said, clock=clock)


def _command(d, **command):
    thread = threading.Thread(target=d.desk.command, args=({'id': 'c', 'run_id': 'r1', **command},))
    thread.start()
    _tick_until(d.tasks, thread)
    thread.join()


def test_a_session_plans_runs_a_step_and_ends_the_task(tmp_path):
    d = _desk(FakeRunner({'navigate_to': [Status.RUNNING, Status.SUCCESS]}), tmp_path)

    assert d.opened[0]['state'] == 'open' and d.opened[0]['task'] == 'Go to the call panel' and 'call_panel' in d.opened[0]['places']

    _command(d, op='plan', subgoals=['walk to the call panel'])
    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk to the call panel')
    _command(d, op='done', reason='arrived')

    assert [reply['ok'] for reply in d.replies] == [True, True, True]
    assert d.replies[1]['status'] == 'success'
    assert (tmp_path / 'plan.md').read_text().startswith('# Task plan, version 1')
    assert next(event for event in d.tracer.events if event['kind'] == 'started')['subgoal'] == 'walk to the call panel'
    assert d.tracer.events[-1]['kind'] == 'task_finished' and d.tracer.events[-1]['end'] == 'done'
    assert d.tracer.events[-1]['reason'] == 'arrived'
    assert d.opened[-1] == {'state': 'closed', 'run_id': 'r1', 'end': 'done'}


def test_commands_for_another_run_or_past_max_steps_are_refused(tmp_path):
    d = _desk(FakeRunner(), tmp_path, max_steps=1)

    _command(d, op='run', skill='say', inputs={'text': 'hi'}, subgoal='greet')
    _command(d, op='run', skill='say', inputs={'text': 'hi'}, subgoal='greet')
    d.desk.command({'id': 'x', 'run_id': 'other', 'op': 'done'})

    assert [reply['ok'] for reply in d.replies] == [True, False, False]
    assert 'max_steps' in d.replies[1]['message'] and 'no open task' in d.replies[2]['message']


def test_a_quiet_session_is_said_once_and_the_time_limit_gives_up(tmp_path):
    d = _desk(FakeRunner(), tmp_path, idle_s=60.0, max_task_s=300.0)

    d.clock.now = 61.0
    d.desk.check()
    d.desk.check()

    assert d.said == [LOST_SPEECH]

    d.clock.now = 301.0
    d.desk.check()

    assert d.tracer.events[-1]['end'] == 'gave_up' and d.opened[-1]['end'] == 'gave_up' and d.desk.task is None


def test_a_cancel_closes_the_open_task(tmp_path):
    d = _desk(FakeRunner(), tmp_path)

    d.tasks.cancel()
    d.desk.check()

    assert d.opened[-1] == {'state': 'closed', 'run_id': 'r1', 'end': 'ended'}


def test_a_long_step_is_not_a_quiet_session(tmp_path):
    d      = _desk(FakeRunner({'navigate_to': [Status.RUNNING] * 3 + [Status.SUCCESS]}), tmp_path, idle_s=60.0)
    thread = threading.Thread(target=d.desk.command, args=({'id': 'c', 'run_id': 'r1', 'op': 'run', 'skill': 'navigate_to',
                                                            'inputs': {'goal': '{call_panel}'}, 'subgoal': 'walk'},))
    thread.start()

    while thread.is_alive():                            # the executor's tick: the step, then the desk's check
        d.clock.now += 30.0
        d.tasks.tick()
        d.desk.check()
        thread.join(0.01)

    d.desk.check()

    assert d.said == [] and d.replies[0]['status'] == 'success'


def test_ending_an_old_task_does_not_close_the_new_one(tmp_path):
    d   = _desk(FakeRunner(), tmp_path)
    old = d.desk.task
    d.desk.open('Say hello', _context())

    d.desk._end(old, 'done', 'late')

    assert d.desk.task is not None and d.desk.task is not old and d.opened[-1]['state'] == 'open'
