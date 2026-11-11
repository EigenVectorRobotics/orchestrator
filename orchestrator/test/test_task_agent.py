import threading

import pytest

from types import SimpleNamespace

from orchestrator.task_agent import LOST_SPEECH, StepGate, TaskDesk, task_summary
from orchestrator.behavior_tree import Status

from test_tasks import _context, _tasks, _tick_until
from test_behavior_tree import FakeClock, FakeRunner


def _desk(runner: FakeRunner, tmp_path, **limits):
    tasks, tracer, _ = _tasks(runner)
    opened, replies  = [], []
    said, clock      = [], FakeClock()
    desk             = TaskDesk(tasks, opened.append, replies.append, said.append, clock=clock, **limits)
    context          = _context()
    context.blackboard['call_panel'] = {'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'frame': 'map', 'kind': 'place', 'floor': 'floor 2'}

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

    assert d.opened[0]['state'] == 'open' and d.opened[0]['task'] == 'Go to the call panel' 
    assert d.opened[0]['places'] == {'call_panel': {'kind': 'place', 'floor': 'floor 2'}}

    _command(d, op='plan', subgoals=['walk to the call panel'])
    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk to the call panel')
    _command(d, op='done', reason='arrived')

    assert [reply['ok'] for reply in d.replies] == [True, True, True]
    assert d.replies[1]['status'] == 'success'
    assert 'walk to the call panel' in (tmp_path / 'plan.md').read_text()
    assert next(event for event in d.tracer.events if event['kind'] == 'started')['subgoal'] == 'walk to the call panel'
    assert d.tracer.events[-1]['kind'] == 'task_finished' and d.tracer.events[-1]['end'] == 'done'
    assert d.tracer.events[-1]['reason'] == 'arrived'
    assert d.opened[-1]['state'] == 'closed' and d.opened[-1]['end'] == 'done'


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


def _gated_run(d, decide):
    """A run command while the gate is on: decide(request) answers it once the request is out"""
    thread = threading.Thread(target=d.desk.command, args=({'id': 'c', 'run_id': 'r1', 'op': 'run', 'skill': 'say',
                                                            'inputs': {'text': 'hi'}, 'subgoal': 'greet'},))
    thread.start()

    while d.desk.gate.request is None and thread.is_alive():
        thread.join(0.01)

    decide(d.desk.gate.request)
    _tick_until(d.tasks, thread)
    thread.join(5.0)
    assert not thread.is_alive(), 'the run still waits at the gate'


def test_the_step_gate_runs_an_approved_step_and_not_a_refused_one(tmp_path):
    runner, requests = FakeRunner(), []
    d                = _desk(runner, tmp_path, gate=StepGate(requests.append))

    _gated_run(d, lambda request: d.desk.gate.decide({'id': request['id'], 'approve': True}))
    _gated_run(d, lambda request: (d.desk.gate.decide({'id': 'other', 'approve': True}),
                                   d.desk.gate.decide({'id': request['id'], 'approve': False, 'reason': 'too close'})))

    assert [reply['status'] for reply in d.replies] == ['success', 'refused']
    assert len(runner.calls) == 1 and 'too close' in d.replies[1]['message']
    assert [request['state'] for request in requests] == ['open', 'approved', 'open', 'refused']
    assert requests[0]['skill'] == 'say' and requests[0]['step'] == 1


def test_a_step_waiting_at_the_gate_is_refused_when_the_task_ends(tmp_path):
    d = _desk(FakeRunner(), tmp_path, gate=StepGate(lambda request: None))

    _gated_run(d, lambda request: (d.tasks.cancel(), d.desk.check()))

    assert d.replies[0]['status'] == 'refused' and 'the task ended' in d.replies[0]['message']


def test_a_new_task_closes_the_old_one_and_refuses_its_waiting_step(tmp_path):
    d = _desk(FakeRunner(), tmp_path, gate=StepGate(lambda request: None))

    _gated_run(d, lambda request: (d.desk.gate.ask({'step': 9}, lambda: True)['approve'] is False
                                   or pytest.fail('a second request was taken'),
                                   d.desk.open('Say hello', _context('r2'))))

    assert d.replies[0]['status'] == 'refused' and 'the task ended' in d.replies[0]['message']
    assert {'state': 'closed', 'run_id': 'r1', 'end': 'replaced'} in d.opened and d.opened[-1]['state'] == 'open'


def test_the_session_gets_the_skills_the_executor_has(tmp_path):
    d = _desk(FakeRunner(), tmp_path)

    d.desk.command({'id': 'k', 'run_id': 'r1', 'op': 'skills'})

    assert d.replies[0]['ok'] and '## navigate_to' in d.replies[0]['catalog']


def test_the_open_task_carries_the_runbook_for_a_session_and_the_loop(tmp_path):
    d = _desk(FakeRunner(), tmp_path, runbook=lambda: 'stanley has no hands wired up')

    assert 'stanley has no hands wired up' in task_summary(d.opened[0])
