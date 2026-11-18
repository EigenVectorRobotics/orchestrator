import sys
import json
import threading

import pytest

from skill_registry import Registry, contract_grammar, parse_contract

from orchestrator.tasks import describe_catalog

from orchestrator.task_loop import MAX_SECTION_CHARS, CodexDecider, DeciderError, TaskLoop, build_prompt
from orchestrator.behavior_tree import Status

from fakes import FakeRunner, _context, _desk, _tick_until

SAY = {'name': 'say', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5.0,
       'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'}}


def _decision(op, **fields):
    return {'reason': '', 'op': op, 'subgoals': [], 'skill': '', 'inputs_json': '', 'subgoal': '',
            'yaml': '', 'code': ''} | fields


class ScriptedDecider:
    """Replies in order; an Exception in the script is raised instead"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def decide(self, prompt, image, work):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)

        if isinstance(reply, Exception):
            raise reply

        return reply


def _loop(d, decider):
    loop   = TaskLoop(d.desk, decider, lambda: 'navigate_to(goal: pose)', lambda: None, d.tracer, retry_s=0.0)
    thread = threading.Thread(target=loop.run, args=(d.opened[0],))
    thread.start()
    _tick_until(d.tasks, thread, max_ticks=400)
    thread.join(timeout=2.0)

    return thread


def test_the_model_plans_runs_a_step_and_ends_the_task(tmp_path):
    d       = _desk(FakeRunner({'navigate_to': [Status.RUNNING, Status.SUCCESS]}), tmp_path)
    decider = ScriptedDecider([_decision('plan', subgoals=['walk to the panel']),
                               _decision('run', skill='navigate_to', inputs_json='{"goal": "{call_panel}"}', subgoal='walk to the panel'),
                               _decision('done', reason='arrived')])

    assert not _loop(d, decider).is_alive()
    assert d.desk.task is None and d.opened[-1]['end'] == 'done'
    assert 'call_panel' in decider.prompts[0] and 'navigate_to' in decider.prompts[0]
    assert 'status: success' in decider.prompts[2]                         # the step's result is in the next prompt


def test_a_bad_decision_or_a_model_error_goes_back_and_the_loop_goes_on(tmp_path):
    d       = _desk(FakeRunner(), tmp_path)
    decider = ScriptedDecider([_decision('run', skill='navigate_to', inputs_json='{"goal": '),
                               _decision('fly', skill='navigate_to'),
                               DeciderError('no network'),
                               _decision('give_up', reason='cannot')])

    assert not _loop(d, decider).is_alive()
    assert 'not JSON' in decider.prompts[1] and 'unknown op fly' in decider.prompts[2] and d.opened[-1]['end'] == 'gave_up'
    assert 'decider_error' in d.tracer.kinds()


def test_a_replaced_task_loop_stops_without_an_error_on_the_new_run(tmp_path):
    d = _desk(FakeRunner(), tmp_path)

    class Replaced:
        def decide(self, prompt, image, work):
            d.desk.open('a new task', _context('r2'))
            raise DeciderError('late answer')

    assert not _loop(d, Replaced()).is_alive()
    assert d.desk.task.run_id == 'r2' and 'decider_error' not in d.tracer.kinds()


def test_a_model_that_keeps_failing_goes_quiet_and_the_robot_says_so(tmp_path):
    d = _desk(FakeRunner(), tmp_path, idle_s=5.0)

    with pytest.raises(DeciderError):
        with d.desk.thinking():
            d.clock.now += 10.0
            raise DeciderError('no network')

    d.desk.check()

    assert d.said                                                          # LOST_SPEECH: no success for idle_s


def test_the_prompt_keeps_the_last_turns_only():
    history = [(_decision('plan', subgoals=[str(n)]), {'ok': True}) for n in range(20)]
    prompt  = build_prompt({'state': 'open', 'run_id': 'r1', 'task': 't'}, '', history)

    assert '(8 earlier turns not shown)' in prompt and 'Turn 20' in prompt and 'Turn 8:' not in prompt


FAKE_CODEX = '''
import sys, json, pathlib
args = sys.argv[1:]
if args[:2] == ['features', 'list']:
    print('shell_tool  stable  true')
    print('apps        stable  true')
    sys.exit(0)
pathlib.Path(sys.argv[0]).with_name('args.json').write_text(json.dumps(args))
prompt = sys.stdin.read()
if 'FAIL' in prompt:
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'model_not_found'}}))
    sys.exit(1)
out = pathlib.Path(args[args.index('--output-last-message') + 1])
out.write_text(json.dumps({'reason': 'r', 'op': 'done', 'subgoals': [], 'skill': '', 'inputs_json': '', 'subgoal': ''}))
'''


def test_codex_runs_read_only_with_the_schema_and_only_known_features_off(tmp_path):
    fake = tmp_path / 'codex'
    fake.write_text(f'#!{sys.executable}\n' + FAKE_CODEX)
    fake.chmod(0o755)
    work = tmp_path / 'turn'
    work.mkdir()

    decision = CodexDecider(model='gpt-6-astra', executable=str(fake)).decide('decide', b'jpeg', work)
    args     = json.loads((tmp_path / 'args.json').read_text())

    assert decision['op'] == 'done' and (work / 'prompt.md').exists()
    assert args[args.index('--sandbox') + 1] == 'read-only' and args[args.index('--model') + 1] == 'gpt-6-astra'
    assert [args[n + 1] for n, arg in enumerate(args) if arg == '--disable'] == ['shell_tool', 'apps']
    assert args[args.index('--image') + 1] == str(work / 'frame.jpg')

    with pytest.raises(DeciderError, match='model_not_found'):            # and the answer of the turn before is not reused
        CodexDecider(executable=str(fake)).decide('FAIL', None, work)

    broken = tmp_path / 'broken'
    broken.write_text('#!/bin/sh\nexit 2\n')
    broken.chmod(0o755)

    with pytest.raises(DeciderError, match='cannot be turned off'):         # no decision with codex's tools on
        CodexDecider(executable=str(broken)).decide('decide', None, work)


def test_the_model_can_write_a_skill_and_the_desk_checks_it_as_it_checks_a_session(tmp_path):
    written = []
    d       = _desk(FakeRunner({}), tmp_path)
    d.desk.writer = type('Writer', (), {'write': lambda self, text, code='': (written.append((text, code)),
                                                                              {'ok': True, 'message': 'added'})[1]})()
    decider = ScriptedDecider([_decision('write_skill', yaml='name: greet\n', code='def run(skills, inputs):\n    return {}\n'),
                               _decision('give_up', reason='enough')])

    assert not _loop(d, decider).is_alive()
    assert written == [('name: greet\n', 'def run(skills, inputs):\n    return {}\n')]


def test_the_prompt_says_how_to_decide_and_the_grammar_says_how_to_write_a_skill():
    opened  = {'state': 'open', 'task': 'x', 'run_id': 'r'}
    prompt  = build_prompt(opened, 'say(text: string)', [])
    writing = build_prompt(opened | {'grammar': contract_grammar()}, 'say(text: string)', [])

    assert 'write_skill' in prompt and 'transport: code' not in prompt           # the shape rides the grammar
    assert 'transport: code' in writing and 'def run(skills, inputs)' in writing


def test_the_prompt_shows_each_part_of_a_result_and_what_the_robot_logged():
    reply  = {'ok': False, 'status': 'failure', 'message': 'the goal was refused',
              'failure': {'skill': 'navigate_to', 'feedback': ['far' * 900]},
              'trace': ['validation: step1_navigate_to: no key nowhere']}
    prompt = build_prompt({'state': 'open', 'run_id': 'r1', 'task': 't'}, '',
                          [(_decision('run', skill='navigate_to'), reply)])
    failure = next(line for line in prompt.splitlines() if line.startswith('  failure:'))

    assert 'message: the goal was refused' in prompt and 'trace: validation:' in prompt   # a long failure keeps both
    assert len(failure) <= MAX_SECTION_CHARS + len('  failure: ')


def test_a_stale_skill_reaches_the_prompt_the_model_reads():
    """The seam: Stamps.reasons -> describe_catalog -> build_prompt. A break here leaves every other test green"""
    catalog = describe_catalog(Registry([parse_contract(SAY)]), {'nuc'}, {'say': 'the code moved under it: nav'})
    prompt  = build_prompt({'state': 'open', 'run_id': 'r1', 'task': 't'}, catalog, [])

    assert 'the code moved under it: nav' in prompt and 'stale' in prompt


def test_the_prompt_stays_within_a_budget_and_keeps_the_newest_turn_whole():
    """The model reads one prompt per decision, up to task_max_steps of them, so the history cannot be unbounded.
    An old turn's detail has been acted on already; the newest turn's has not"""
    reply   = {'ok': False, 'status': 'failure', 'message': 'the goal was refused',
               'outputs': {'where': 'x' * 2000}, 'failure': {'feedback': ['far' * 900]},
               'trace': [f'validation: line {turn} ' + 'z' * 400 for turn in range(12)]}
    history = [(_decision('run', skill=f'skill{turn}'), reply) for turn in range(12)]
    prompt  = build_prompt({'state': 'open', 'run_id': 'r1', 'task': 't'}, '', history)

    assert len(prompt) < 20_000, f'the history alone is {len(prompt)} characters'
    assert 'skill11' in prompt and 'the goal was refused' in prompt


def test_a_collapsed_turn_still_says_that_it_failed():
    """The agent decides whether to run a skill again from the older turns. A step whose message does not say it
    failed (press_button reports 'pressed' and the check refuses it) must not read like one that worked"""
    pressed = (_decision('run', skill='press_button'), {'ok': False, 'status': 'failure', 'message': 'pressed'})
    said    = (_decision('run', skill='say'), {'ok': True, 'status': 'success', 'message': 'said'})
    prompt  = build_prompt({'state': 'open', 'run_id': 'r1', 'task': 't'}, '', [pressed, said, said, said])
    turn    = prompt.split('Turn 1:')[1].split('Turn 2:')[0]          # old enough to be collapsed to one line

    assert 'press_button' in turn and 'failure' in turn
