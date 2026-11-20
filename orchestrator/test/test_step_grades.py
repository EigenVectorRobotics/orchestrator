import json
import threading

from orchestrator.behavior_tree import Status
from orchestrator.knowledge import KnowledgeStore
from orchestrator.step_grades import StepGrades

from fakes import FakeRunner, _context, _desk, _tick_until


def _command(d, **command):
    """A command on its own thread while the tasks tick, as the executor runs it"""
    thread = threading.Thread(target=d.desk.command, args=({'id': 'x', 'run_id': 'r1', **command},))
    thread.start()
    _tick_until(d.tasks, thread)
    thread.join()


def _logged(steps: StepGrades) -> list[tuple]:
    return [(entry['run'], entry['step'], entry['skill'], entry['version'], entry['grade'])
            for entry in map(json.loads, steps.log_path.read_text().splitlines())]


class RewrittenWhileRunning(FakeRunner):
    """The agent saves a new version of the skill while its step runs"""

    def __init__(self, versions: dict, *args):
        super().__init__(*args)
        self.versions = versions

    def start(self, contract, inputs):
        self.versions[contract.name] = f'v{len(self.calls) + 2}'
        return super().start(contract, inputs)


def test_each_step_s_skill_is_graded_for_the_version_that_ran_and_kept_only_at_the_commit(tmp_path):
    versions = {'navigate_to': 'v1'}
    steps    = StepGrades(tmp_path / 'knowledge', version=versions.get)
    d        = _desk(RewrittenWhileRunning(versions, {'navigate_to': [[Status.SUCCESS], [Status.FAILURE]]}), tmp_path,
                     steps=steps)

    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk')
    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk again')
    _command(d, op='grade_step', step=1, grade='worked', reason='it stood at the panel in the image')
    _command(d, op='grade_step', step=3, grade='worked', reason='no such step')
    _command(d, op='grade_step', step=1, grade='useful', reason='a claim grade')
    _command(d, op='grade_step', step=True, grade='failed', reason='not a step number')
    _command(d, op='commit')
    assert not steps.log_path.exists()                                   # nothing kept before the commit
    _command(d, op='done', reason='at the panel')
    _command(d, op='commit')

    assert [reply['ok'] for reply in d.replies] == [True, True, True, False, False, False, False, True, True]
    assert 'grade-step 1' in d.replies[0]['grade'] and 'nothing to grade' in d.replies[3]['message']
    assert 'not a step grade' in d.replies[4]['message'] and 'whole number' in d.replies[5]['message']
    assert 'still open' in d.replies[6]['message'] and d.replies[7]['message'].endswith('not graded: step 2')
    assert _logged(steps) == [('r1', 1, 'navigate_to', 'v1', 'worked')] and d.replies[8]['not_graded'] == ['step 2']
    assert 'grade-step' in d.opened[0]['known'] and steps.staged('r1') == {}
    d.desk.open('Learn the stack', _context('r2'), discovery=True)
    assert 'grade-step' not in d.opened[-1]['known']                    # a discovery task runs no step


def test_a_claims_file_that_cannot_be_committed_does_not_stop_the_step_grades(tmp_path):
    store = KnowledgeStore(tmp_path / 'knowledge')
    steps = StepGrades(tmp_path / 'knowledge')
    d     = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path, knowledge=store, steps=steps)

    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk')
    _command(d, op='grade_step', step=1, grade='worked', reason=' ')
    _command(d, op='grade_step', step=1, grade='worked', reason='it arrived')
    _command(d, op='done', reason='arrived')
    store.staged_dir.mkdir()
    (store.staged_dir / 'r1.json').write_text('{"C-0001": ')                    # torn
    _command(d, op='commit')

    assert _logged(steps) == [('r1', 1, 'navigate_to', None, 'worked')] and (store.staged_dir / 'r1.bad').exists()
    assert not d.replies[-1]['ok'] and 'staged/r1' in d.replies[-1]['message'] and not steps.has_staged('r1')
    assert 'needs a reason' in d.replies[1]['message']


def test_grades_left_after_a_task_go_in_once_at_the_next_open_and_a_torn_file_is_set_aside(tmp_path):
    steps = StepGrades(tmp_path / 'knowledge')
    store = KnowledgeStore(tmp_path / 'knowledge')
    d     = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path, steps=steps, knowledge=store)

    _command(d, op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk')
    _command(d, op='done', reason='arrived')
    _command(d, op='grade_step', step=1, grade='blocked', reason='the Orin was down')
    left = steps.staged('r1')
    d.desk.open('Go back', _context('r2'))
    steps._write_staged('r1', left)                                             # a crash after the log write, before the unlink
    (steps.staged_dir / 'r0.json').write_text('{"step 1": {"grade": "worked"')        # torn: it cannot be read
    d.desk.open('Go back again', _context('r3'))

    assert _logged(steps) == [('r1', 1, 'navigate_to', None, 'blocked')] and steps.staged_runs() == []
    assert (steps.staged_dir / 'r0.bad').exists() and steps.set_aside('r9') is None   # set aside once; no file: nothing
    assert not store.claims_path.exists()                               # no claim was staged: the claims are not written
