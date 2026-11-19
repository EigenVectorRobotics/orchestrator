import threading

import pytest

from types import SimpleNamespace

from skill_registry import contract_grammar

from orchestrator.knowledge import KnowledgeStore, Sighting
from orchestrator.task_desk import LOST_SPEECH, task_summary
from orchestrator.step_gate import StepGate
from orchestrator.behavior_tree import Status

from fakes import FakeRunner, _context, _desk, _tick_until


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
    refused = [event for event in d.tracer.events if event['kind'] == 'step_gate'][-1]  # the page puts it in the plan
    assert (refused['state'], refused['subgoal'], refused['step']) == ('refused', 'greet', 2)


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


def test_the_open_task_carries_the_runbook_and_the_grammar_for_a_session_and_the_loop(tmp_path):
    d       = _desk(FakeRunner(), tmp_path, runbook=lambda: 'stanley has no hands wired up')
    writing = _desk(FakeRunner(), tmp_path, writer=SimpleNamespace())

    assert 'stanley has no hands wired up' in task_summary(d.opened[0])
    assert contract_grammar() not in task_summary(d.opened[0])              # write_skill is off without a writer
    assert contract_grammar() in task_summary(writing.opened[0])


def test_a_step_that_does_not_validate_says_why_and_what_was_logged(tmp_path):
    d = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path)

    _command(d, op='run', skill='navigate_to', inputs={'goal': '{nowhere}'}, subgoal='walk')

    assert d.replies[-1]['status'] == 'error' and 'nowhere' in d.replies[-1]['message']
    assert any('rejected' in line for line in d.replies[-1]['trace'])      # the tap: an event reaches the agent too


def test_a_step_says_its_skill_is_stale_and_keeps_that_it_ran(tmp_path):
    """The seam: the executor hands the desk the stamps (stale, ran). Both have harmless defaults, so a mis-wire
    is silent: the agent is never warned and no run is ever kept"""
    kept = []
    d    = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path,
                 stale=lambda: {'navigate_to': 'the code moved under it: nav'},
                 ran=lambda skill, run: kept.append((skill, run)))

    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk')

    assert d.replies[-1]['status'] == 'success' and kept == [('navigate_to', 'r1')]
    assert any('stale_skill' in line for line in d.replies[-1]['trace'])     # in the trace, not only in the prompt



def _claims(tmp_path, *texts):
    store = KnowledgeStore(tmp_path / 'knowledge')
    return store, [store.note(text, Sighting(by='agent:task', time=1.0, run='r0'), {'skill': 'navigate_to'})[0].id
                   for text in texts]


def test_grades_are_staged_and_go_into_the_store_only_at_the_commit_after_the_task(tmp_path):
    store, (lesson, place, other) = _claims(tmp_path, 'When it stops short, walk again.', 'The panel is left.', 'Small.')
    d = _desk(FakeRunner({'navigate_to': [Status.FAILURE]}), tmp_path, knowledge=store)

    _command(d, op='grade', claim=lesson, grade='useful', reason='no step followed it yet')
    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk', applies=[lesson, place])
    _command(d, op='grade', claim=lesson, grade='useful', reason='a first thought')
    _command(d, op='grade', claim=lesson, grade='wrong', reason='walking again did not help')    # the last one wins
    _command(d, op='grade', claim=other, grade='useful', reason='no step followed it')
    _command(d, op='commit')
    assert store.get(lesson).sightings == store.get(lesson).confirmations[:1]  # nothing in the store before the commit
    _command(d, op='done', reason='it did not arrive')
    _command(d, op='commit')

    assert [reply['ok'] for reply in d.replies] == [False, True, True, True, False, False, True, True]
    assert 'nothing staged' in d.replies[0]['message'] and 'no step of this run followed' in d.replies[4]['message']
    assert 'still open' in d.replies[5]['message'] and d.replies[6]['message'].endswith(f'not graded: {place}')
    wrong = store.get(lesson).contradictions[0]
    assert (wrong.by, wrong.grade, wrong.evidence) == ('outcome:navigate_to', 'wrong', ['step:1'])
    assert len(store.get(lesson).confirmations) == 1 and d.replies[7]['not_graded'] == [place]
    assert 'agent_cli commit' in d.opened[0]['known'] and 'knowledge note' in d.opened[0]['known']


def test_a_grade_after_the_end_goes_in_when_the_next_task_opens(tmp_path):
    store, (lesson,) = _claims(tmp_path, 'When it stops short, walk again.')
    d = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path, knowledge=store)

    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk', applies=[lesson])
    _command(d, op='done', reason='arrived')
    _command(d, op='grade', claim=lesson, grade='useful', reason='it walked again and arrived')
    assert len(store.get(lesson).confirmations) == 1

    d.desk.open('Go back', _context('r2'))
    _command(d, op='grade', claim=lesson, grade='wrong', reason='too late: r1 is committed')

    assert [s.grade for s in store.get(lesson).confirmations] == [None, 'useful'] and not d.replies[-1]['ok']


def test_a_step_that_follows_an_unknown_claim_does_not_run(tmp_path):
    store, (lesson,) = _claims(tmp_path, 'When it stops short, walk again.')
    d = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path, knowledge=store)

    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk', applies=[lesson, 'C-9999'])
    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk', applies=lesson)

    assert not d.replies[0]['ok'] and 'no claim C-9999' in d.replies[0]['message']
    assert not d.replies[1]['ok'] and 'a list of claim ids' in d.replies[1]['message'] and d.tasks.task.steps == 0


def test_a_grade_needs_a_reason_and_the_open_or_last_run(tmp_path):
    store, (lesson,) = _claims(tmp_path, 'When it stops short, walk again.')
    d = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path, knowledge=store)

    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk', applies=[lesson])
    _command(d, op='grade', claim=lesson, grade='useful', reason=' ')
    d.desk.command({'id': 'x', 'run_id': '../../x', 'op': 'commit'})

    assert 'needs a reason' in d.replies[1]['message'] and 'not the open task' in d.replies[2]['message']


def test_a_staged_file_that_cannot_be_committed_is_set_aside_and_the_next_task_opens(tmp_path):
    store, (lesson,) = _claims(tmp_path, 'When it stops short, walk again.')
    d = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path, knowledge=store)
    store.staged_dir.mkdir()
    (store.staged_dir / 'r0.json').write_text('{"C-0001": {"no": "such field"}}')

    d.desk.open('Go back', _context('r2'))

    assert d.opened[-1]['state'] == 'open' and (store.staged_dir / 'r0.bad').exists() and store.staged_runs() == []


def test_a_discovery_task_runs_no_skill_and_has_no_time_limit_or_idle_speech(tmp_path):
    d = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path, idle_s=60.0, max_task_s=300.0)
    d.desk.open('Learn the stack', _context('r2'), discovery=True)

    d.desk.command({'id': 'x', 'run_id': 'r2', 'op': 'run', 'skill': 'navigate_to', 'inputs': {}, 'subgoal': 'walk'})
    d.clock.now = 1000.0
    d.desk.check()

    assert not d.replies[0]['ok'] and 'never moves the robot' in d.replies[0]['message'] and d.tasks.task.steps == 0
    assert d.opened[-1]['discovery'] and 'discovery' in task_summary(d.opened[-1]) and d.said == []
    assert d.desk.task is not None                                          # still open after max_task_s
