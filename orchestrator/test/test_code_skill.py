"""A code skill: the sandbox decides, the executor does every call, and the gate list still stops the ones that move"""

import time

import pytest

from skill_registry import Registry, parse_contract

from orchestrator.behavior_tree import Context, Status
from orchestrator.code_skill import CodeRunner
from orchestrator.sandbox import SandboxConfig
from orchestrator.skill_leaf import DoneHandle, make_leaf_factory


SAY = {'name': 'say', 'kind': 'action', 'device': 'nuc', 'description': 'Say a sentence', 'timeout_s': 2,
       'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'},
       'inputs': [{'name': 'text', 'type': 'string', 'field': 'data'}]}

WALK = {'name': 'navigate_to', 'kind': 'action', 'device': 'nuc', 'description': 'Walk to a goal', 'timeout_s': 5,
        'interface': {'transport': 'action', 'name': '/route_planner/navigate_to_goal', 'ros_type': 'a/action/B'},
        'inputs': [{'name': 'goal', 'type': 'pose', 'field': 'goal'}]}

CODE = {'name': 'where_am_i', 'kind': 'action', 'device': 'nuc', 'description': 'Say where the robot is',
        'timeout_s': 20, 'interface': {'transport': 'code'}}

OFFSET = {'name': 'set_offset', 'kind': 'action', 'device': 'nuc', 'description': 'Move the press goal', 'timeout_s': 2,
          'interface': {'transport': 'topic', 'name': '/offset', 'ros_type': 'a/msg/B'},
          'inputs': [{'name': 'depth', 'type': 'float', 'field': 'depth'},
                     {'name': 'tries', 'type': 'int', 'field': 'tries'},
                     {'name': 'left', 'type': 'bool', 'field': 'left'},
                     {'name': 'goal', 'type': 'pose', 'field': 'goal', 'required': False}]}

SERVO = {'name': 'servo_press', 'kind': 'action', 'device': 'nuc', 'description': 'Press, looking as it goes',
         'timeout_s': 20, 'interface': {'transport': 'code'}, 'outputs': [{'name': 'tries', 'type': 'int'}]}


class FakeRunner:
    """Every skill succeeds, unless its name is in fails"""

    def __init__(self, fails=()):
        self.calls = []
        self.fails = set(fails)

    def start(self, contract, inputs):
        self.calls.append((contract.name, inputs))

        if contract.name in self.fails:
            return DoneHandle(False, 'no server')

        return DoneHandle(True, 'sent', {'answer': 'yes'})


def _context(blackboard=None):
    records = []
    context = Context(blackboard=blackboard or {}, on_record=records.append)
    context.records = records

    return context


def _runner(tmp_path, code: str, runner=None, approve=None) -> CodeRunner:
    (tmp_path / 'skills' / 'where_am_i').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'skills' / 'where_am_i' / 'code.py').write_text(code)

    registry = Registry([parse_contract(SAY), parse_contract(WALK), parse_contract(CODE)])
    skills   = CodeRunner(registry, SandboxConfig(scratch_dir=str(tmp_path / 'scratch'), timeout_s=20),
                          skills_dir=str(tmp_path / 'skills'), **({'approve': approve} if approve else {}))
    skills.runner     = runner or FakeRunner()
    skills.build_leaf = make_leaf_factory(registry, skills.runner, code=skills)

    return skills


def _wait_until(done, seconds: float) -> bool:
    deadline = time.monotonic() + seconds

    while not done() and time.monotonic() < deadline:
        time.sleep(0.02)

    return done()


def _finish(handle, seconds=20.0) -> Status:
    _wait_until(lambda: handle.poll() != Status.RUNNING, seconds)

    return handle.poll()


def test_the_code_calls_a_skill_and_returns_its_outputs(tmp_path):
    code    = _runner(tmp_path, 'def run(skills, inputs):\n'
                                '    result = skills.run("say", text=f"hello {inputs[\'who\']}")\n'
                                '    return {"said": result.ok, "answer": result.outputs["answer"]}\n')
    context = _context()
    handle  = code.start(code.registry.get('where_am_i'), {'who': 'world'}, context)

    assert _finish(handle) == Status.SUCCESS
    assert handle.outputs == {'said': True, 'answer': 'yes'}
    assert code.runner.calls == [('say', {'text': 'hello world'})]
    assert [record['skill'] for record in context.records] == ['say']
    assert context.records[0]['path'] == 'where_am_i/say'       # the record says it ran inside the code skill


def test_a_failed_call_comes_back_to_the_code_which_decides_what_to_do(tmp_path):
    code   = _runner(tmp_path, 'def run(skills, inputs):\n'
                               '    if not skills.run("say", text="first"):\n'
                               '        skills.run("say", text="second")\n'
                               '    return {}\n',
                     runner=FakeRunner(fails={'say'}))
    handle = code.start(code.registry.get('where_am_i'), {}, _context())

    assert _finish(handle) == Status.SUCCESS
    assert len(code.runner.calls) == 2


def test_the_code_reads_a_place_from_the_blackboard(tmp_path):
    code    = _runner(tmp_path, 'def run(skills, inputs):\n'
                                '    skills.run("navigate_to", goal="{panel}")\n'
                                '    return {}\n',
                      approve=lambda contract, inputs, reasons: (True, ''))
    context = _context({'panel': {'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'frame': 'map'}})
    handle  = code.start(code.registry.get('where_am_i'), {}, context)

    assert _finish(handle) == Status.SUCCESS
    assert code.runner.calls[0][1]['goal']['x'] == 1.0


def test_a_skill_the_gate_list_names_asks_again_inside_the_code(tmp_path):
    asked = []
    code  = _runner(tmp_path, 'def run(skills, inputs):\n'
                              '    walked = skills.run("navigate_to", goal="1 2 0")\n'
                              '    skills.run("say", text="done")\n'
                              '    return {"walked": walked.ok, "why": walked.message}\n',
                    approve=lambda contract, inputs, reasons: (asked.append(contract.name), (False, 'not now'))[1])
    handle = code.start(code.registry.get('where_am_i'), {}, _context())

    assert _finish(handle) == Status.SUCCESS
    assert asked == ['navigate_to']                        # say moves nothing, so it is not asked
    assert handle.outputs['walked'] is False
    assert 'not now' in handle.outputs['why']
    assert [name for name, _ in code.runner.calls] == ['say']


def test_the_code_cannot_reach_ros_or_call_itself(tmp_path):
    code   = _runner(tmp_path, 'def run(skills, inputs):\n'
                               '    own = skills.run("where_am_i")\n'
                               '    try:\n'
                               '        import rclpy\n'
                               '        reached = True\n'
                               '    except ImportError:\n'
                               '        reached = False\n'
                               '    return {"own": own.message, "reached_ros": reached}\n')
    handle = code.start(code.registry.get('where_am_i'), {}, _context())

    assert _finish(handle) == Status.SUCCESS
    assert handle.outputs['reached_ros'] is False
    assert 'cannot call itself' in handle.outputs['own']


def test_one_code_skill_builds_on_another(tmp_path):
    """approach_panel corrects the goal, then hands over to the servo policy, which is a code skill too"""
    code = _runner(tmp_path, 'def run(skills, inputs):\n'
                             '    skills.run("navigate_to", goal="1 2 0")\n'
                             '    pressed = skills.run("servo_press")\n'
                             '    return {"pressed": pressed.ok, "tries": pressed.outputs.get("tries")}\n',
                   approve=lambda contract, inputs, reasons: (True, ''))
    code.registry.add(parse_contract(SERVO))
    (tmp_path / 'skills' / 'servo_press').mkdir()
    (tmp_path / 'skills' / 'servo_press' / 'code.py').write_text('def run(skills, inputs):\n'
                                                      '    for attempt in range(1, 3):\n'
                                                      '        if skills.run("say", text=f"press {attempt}"):\n'
                                                      '            return {"tries": attempt}\n'
                                                      '    return {"tries": 0}\n')
    context = _context()
    handle  = code.start(code.registry.get('where_am_i'), {}, context)

    assert _finish(handle) == Status.SUCCESS
    assert handle.outputs == {'pressed': True, 'tries': 1}
    assert [record['path'] for record in context.records] == ['where_am_i/navigate_to',
                                                              'where_am_i/servo_press/say',
                                                              'where_am_i/servo_press']


def test_a_policy_that_calls_itself_through_another_is_refused(tmp_path):
    """where_am_i -> servo_press -> where_am_i: the second call of the same skill is the one that is refused"""
    code = _runner(tmp_path, 'def run(skills, inputs):\n'
                             '    return skills.run("servo_press").outputs\n')
    code.registry.add(parse_contract(SERVO))
    (tmp_path / 'skills' / 'servo_press').mkdir()
    (tmp_path / 'skills' / 'servo_press' / 'code.py').write_text('def run(skills, inputs):\n'
                                                      '    return {"why": skills.run("where_am_i").message}\n')
    handle = code.start(code.registry.get('where_am_i'), {}, _context())

    assert _finish(handle) == Status.SUCCESS
    assert 'cannot call itself' in handle.outputs['why']


def test_an_unknown_skill_and_a_bad_input_come_back_as_failures(tmp_path):
    code   = _runner(tmp_path, 'def run(skills, inputs):\n'
                               '    return {"unknown": skills.run("fly").message,\n'
                               '            "bad": skills.run("say").message}\n')
    handle = code.start(code.registry.get('where_am_i'), {}, _context())

    assert _finish(handle) == Status.SUCCESS
    assert 'unknown skill fly' in handle.outputs['unknown']
    assert 'text' in handle.outputs['bad']


def test_code_that_raises_fails_the_skill_with_its_traceback(tmp_path):
    code   = _runner(tmp_path, 'def run(skills, inputs):\n    raise ValueError("no pose yet")\n')
    handle = code.start(code.registry.get('where_am_i'), {}, _context())

    assert _finish(handle) == Status.FAILURE
    assert 'no pose yet' in handle.message


def test_a_missing_code_file_fails_before_anything_starts(tmp_path):
    code   = _runner(tmp_path, 'def run(skills, inputs):\n    return {}\n')
    (tmp_path / 'skills' / 'where_am_i' / 'code.py').unlink()
    handle = code.start(code.registry.get('where_am_i'), {}, _context())

    assert handle.poll() == Status.FAILURE
    assert 'no code file' in handle.message


def test_cancel_stops_the_code_and_its_call(tmp_path):
    code    = _runner(tmp_path, 'def run(skills, inputs):\n'
                                '    import time\n'
                                '    open("started", "w").close()\n'
                                '    time.sleep(30)\n'
                                '    return {}\n')
    handle  = code.start(code.registry.get('where_am_i'), {}, _context())
    started = tmp_path / 'scratch' / 'started'          # the code's folder is the sandbox scratch

    assert _wait_until(started.exists, 10.0), 'the code did not start'
    handle.cancel()

    assert _finish(handle, 5.0) in (Status.FAILURE, Status.SUCCESS)
    assert handle.poll() != Status.RUNNING


def test_the_code_passes_numbers_and_poses_not_only_text(tmp_path):
    """A tree gives a leaf XML text; a code skill gives it the value itself, which must not be parsed as text"""
    code = _runner(tmp_path, 'def run(skills, inputs):\n'
                             '    done = skills.run("set_offset", depth=0.05 * 2, tries=3, left=True,\n'
                             '                      goal={"x": 1.0, "y": 2.0, "yaw": 0.0, "frame": "map"})\n'
                             '    return {"ok": done.ok, "why": done.message}\n',
                   approve=lambda contract, inputs, reasons: (True, ''))
    code.registry.add(parse_contract(OFFSET))
    handle = code.start(code.registry.get('where_am_i'), {}, _context())

    assert _finish(handle) == Status.SUCCESS
    assert handle.outputs['ok'] is True, handle.outputs['why']
    assert code.runner.calls[0][1] == {'depth': 0.1, 'tries': 3, 'left': True,
                                       'goal': {'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'frame': 'map'}}
