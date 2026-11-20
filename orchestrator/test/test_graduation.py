import json
import os
import threading

import yaml

from orchestrator.behavior_tree import Status
from orchestrator.graduation import Graduation, earn, effective
from orchestrator.mission_control import SkillShelf
from orchestrator.step_grades import StepGrades

from fakes import FakeRunner, _context, _desk, _tick_until


def test_a_skill_earns_its_maturity_from_its_grades_and_a_failure_in_the_last_ten_takes_tested_away():
    assert [earn(outcomes) for outcomes in (
        [True] * 4,                              # too few
        [True] * 5,                              # 5 of 5: lower bound 0.75
        [True] * 4 + [False] * 2,                # 4 of 6: 0.43, not reliable yet
        [True] * 10,                             # 10 of 10: 0.86
        [True] * 9,                              # 9 of 9 is 0.85, but tested needs 10 uses
        [True] * 40 + [False] + [True] * 9,      # 49 of 50 is 0.94, but one failure in the last 10
        [True] * 30 + [False] + [True] * 10,     # the failure is older than the last 10
        [False] * 10,                            # reliably bad: upper bound 0.14
        [True] + [False] * 9,                    # bad, but not sure yet: upper bound 0.28
    )] == ['untested', 'wip', 'untested', 'tested', 'wip', 'wip', 'tested', 'deprecated', 'untested']

    assert [effective(written, earned) for written, earned in (('tested', 'untested'), ('untested', 'wip'),
                                                               ('tested', 'deprecated'))] == ['tested', 'wip', 'deprecated']


def test_only_robot_grades_of_the_version_on_disk_count_and_the_log_is_read_again_when_it_changes(tmp_path):
    versions = {'walk': 'v2'}
    steps    = StepGrades(tmp_path, version=versions.get)
    log      = steps.log_path
    worked = {'skill': 'walk', 'version': 'v2', 'robot': True, 'grade': 'worked'}
    lines  = [worked | {'time': 10.0 + n} for n in range(4)] + [
              worked | {'robot': False, 'time': 20.0},                                  # the fake robot
              worked | {'version': 'v1', 'time': 21.0},                                 # an older version
              worked | {'grade': 'misused', 'time': 22.0},                              # no evidence
              worked | {'grade': ['worked'], 'time': 23.0},                             # not a grade: left out
              worked | {'time': None}]                                                  # no time: left out
    log.write_text('\n'.join(map(json.dumps, lines)) + '\n{"skill": "wa')              # a torn last line
    graduation = Graduation(steps)

    assert graduation.of('walk', 'untested') == {'maturity': 'untested', 'earned': 'untested', 'worked': 4, 'failed': 0, 'capped_by': ''}
    assert graduation.of('walk', 'tested')['maturity'] == 'tested'                    # a person's word stands

    log.write_text('\n'.join(map(json.dumps, lines + [worked | {'time': 30.0}])) + '\n')
    os.utime(log, ns=(1, 1))                                                           # a new mtime, whatever the clock
    assert graduation.of('walk', 'untested') == {'maturity': 'wip', 'earned': 'wip', 'worked': 5, 'failed': 0, 'capped_by': ''}

    versions['walk'] = 'v3'                                                            # rewritten: it starts again
    assert graduation.of('walk', 'untested')['worked'] == 0

    versions['walk'] = 'v2'                                                            # an old failure, committed last
    log.write_text('\n'.join(map(json.dumps, [worked | {'time': 100.0 + n} for n in range(20)]
                                             + [worked | {'grade': 'failed', 'time': 1.0}])) + '\n')
    os.utime(log, ns=(2, 2))
    assert graduation.of('walk', 'untested')['earned'] == 'tested'                    # the last 10 by time all worked

    def gone(name):
        raise FileNotFoundError(name)

    steps.version = gone                                                               # removed while the tab read it
    assert graduation.of('walk', 'wip') == {'maturity': 'wip', 'earned': 'untested', 'worked': 0, 'failed': 0, 'capped_by': ''}


def test_the_catalog_and_the_skills_tab_show_what_a_skill_earned_on_the_robot(tmp_path):
    steps = StepGrades(tmp_path / 'knowledge', version=lambda name: 'v1', robot=True)
    d     = _desk(FakeRunner({'navigate_to': [Status.SUCCESS]}), tmp_path, steps=steps, graduation=Graduation(steps))

    def command(**message):
        thread = threading.Thread(target=d.desk.command, args=({'id': 'x', 'run_id': 'r1', **message},))
        thread.start()
        _tick_until(d.tasks, thread)
        thread.join()

    for _ in range(5):
        command(op='run', skill='navigate_to', inputs={'goal': '{call_panel}'}, subgoal='walk')

    for step in range(1, 6):
        command(op='grade_step', step=step, grade='worked', reason='it arrived')

    command(op='done', reason='arrived')
    d.desk.open('Go back', _context('r2'))                              # its open commits the grades r1 left
    d.desk.command({'id': 'x', 'run_id': 'r2', 'op': 'skills'})
    assert 'maturity: wip (graded on the robot, this version: 5 worked, 0 failed)' in d.replies[-1]['catalog']

    library = tmp_path / 'skills'
    (library / 'navigate_to').mkdir(parents=True)
    (library / 'navigate_to' / 'contract.yaml').write_text(yaml.safe_dump(
        {'name': 'navigate_to', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5, 'maturity': 'untested',
         'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'}}))
    item = SkillShelf(library, None, tmp_path / 'removed', graduation=Graduation(steps)).get()[1]['skills'][0]

    assert (item['maturity'], item['earned']) == ('wip', {'maturity': 'wip', 'earned': 'wip', 'worked': 5, 'failed': 0, 'capped_by': ''})


def test_a_step_on_the_fake_robot_says_so_in_its_grade(tmp_path):
    fake = StepGrades(tmp_path)                                          # robot: False unless the executor says so
    fake.stage_step('r1', 1, 'navigate_to', 'v1', 'success')

    assert fake.staged('r1')['step 1']['robot'] is False


def test_a_composition_is_never_more_trusted_than_its_weakest_part(tmp_path):
    steps   = StepGrades(tmp_path, version=lambda name: 'v1')
    written = {'ride': 'tested', 'walk': 'wip', 'say': 'tested'}
    parts   = {'ride': {'calls': ['say', 'walk', 'gone']}}                  # gone: a part the library does not have
    graduation = Graduation(steps, composition=lambda name: parts.get(name, {}), written_of=written.get)

    assert (graduation.of('ride', 'tested')['maturity'], graduation.of('ride', 'tested')['capped_by']) == ('wip', 'walk')
    assert graduation.of('say', 'tested')['capped_by'] == ''

    steps.log_path.write_text('\n'.join(json.dumps({'skill': 'walk', 'version': 'v1', 'robot': True, 'grade': 'failed',
                                                    'time': float(n)}) for n in range(10)) + '\n')
    assert graduation.of('walk', 'wip')['maturity'] == 'deprecated'
    assert graduation.of('ride', 'tested')['maturity'] == 'untested'        # a deprecated part counts as untested
    assert graduation.of('ride', 'tested', {'say': 'tested'}.get)['maturity'] == 'tested'   # the caller's list: no walk

    def unreadable(name):
        raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

    steps.version = unreadable                                               # one file: never the whole catalog
    assert graduation.of('say', 'tested')['earned'] == 'untested'            # and no grade vouches for it
