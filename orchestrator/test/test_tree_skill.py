"""A tree skill: a BehaviorTree of skills that runs as one skill, and says which part failed"""

import time

from skill_registry import Registry, parse_contract

from orchestrator.behavior_tree import Status
from orchestrator.skill_leaf import make_leaf_factory
from orchestrator.tree_skill import TreeRunner

from test_code_skill import SAY, WALK, FakeRunner, _context


WALK_AND_GREET = {'name': 'walk_and_greet', 'kind': 'action', 'device': 'nuc', 'timeout_s': 20,
                  'interface': {'transport': 'tree'}, 'inputs': [{'name': 'text', 'type': 'string'}],
                  'outputs': [{'name': 'answer', 'type': 'string'}]}


def _xml(body: str) -> str:
    return f'<root BTCPP_format="4"><BehaviorTree ID="main">{body}</BehaviorTree></root>'


READY = {'name': 'is_ready', 'kind': 'condition', 'device': 'nuc', 'interface': {'transport': 'compare'},
         'inputs': [{'name': 'value', 'type': 'string'}, {'name': 'equals', 'type': 'string'}]}


def _trees(tmp_path, trees: dict, fails=()):
    """A library with these tree skills (name -> XML), and the leaf factory that runs them"""
    contracts = [parse_contract(SAY), parse_contract(WALK), parse_contract(READY)]

    for name, xml in trees.items():
        contracts.append(parse_contract(WALK_AND_GREET | {'name': name}))
        (tmp_path / name).mkdir(parents=True)
        (tmp_path / name / 'tree.xml').write_text(xml)

    registry, runner = Registry(contracts), FakeRunner(fails)
    runner_of_trees  = TreeRunner(registry, str(tmp_path))
    runner_of_trees.build_leaf = make_leaf_factory(registry, runner, tree=runner_of_trees)

    return runner_of_trees.build_leaf, runner


def _run(leaf) -> Status:
    for _ in range(200):
        if (status := leaf.tick()) != Status.RUNNING:
            return status

        time.sleep(0.005)

    raise AssertionError('the tree did not end')


GREET = _xml('<Sequence><navigate_to goal="{panel}"/><say text="{text}"/>'
             '<SetBlackboard value="done" output_key="answer"/><SetBlackboard value="x" output_key="kept_inside"/></Sequence>')


def test_a_tree_runs_its_parts_as_one_skill_with_its_inputs_the_places_and_only_its_outputs_back(tmp_path):
    build, runner = _trees(tmp_path, {'walk_and_greet': GREET})
    context       = _context({'panel': {'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'frame': 'map'}})
    leaf          = build('walk_and_greet', 'step1/walk_and_greet', {'text': 'hello'}, context)

    assert _run(leaf) == Status.SUCCESS
    assert [name for name, _ in runner.calls] == ['navigate_to', 'say'] and runner.calls[1][1] == {'text': 'hello'}
    assert runner.calls[0][1]['goal']['x'] == 1.0                         # it sees the places of the task
    assert leaf.outputs == {'answer': 'done'} and 'kept_inside' not in context.blackboard
    assert [record['skill'] for record in context.records] == ['navigate_to', 'say', 'walk_and_greet']
    assert context.records[1]['path'].startswith('step1/walk_and_greet/')     # each part is recorded, inside the tree


def test_a_failed_tree_names_the_part_that_failed(tmp_path):
    build, _ = _trees(tmp_path, {'walk_and_greet': GREET}, fails=['say'])
    leaf     = build('walk_and_greet', 'step1/walk_and_greet', {'text': 'hello'}, _context({'panel': '1 2 0'}))

    assert _run(leaf) == Status.FAILURE
    assert leaf.message.startswith('say failed at step1/walk_and_greet/') and 'no server' in leaf.message
    assert leaf.outputs['failed_part']['skill'] == 'say' and leaf.outputs['failed_part']['message'] == 'no server'


def test_the_failed_part_is_the_one_the_tree_ended_on_not_one_a_fallback_recovered(tmp_path):
    recovered = '<Fallback><navigate_to goal="{panel}"/><say text="instead"/></Fallback>'
    build, _  = _trees(tmp_path, {'walk_and_greet': _xml(f'<Sequence>{recovered}<say text="{{text}}"/></Sequence>'),
                                  'check_after': _xml(f'<Sequence>{recovered}<is_ready value="a" equals="b"/></Sequence>')},
                       fails=['navigate_to', 'is_ready'])
    context   = _context({'panel': '1 2 0'})
    leaf      = build('walk_and_greet', 'step1/walk_and_greet', {'text': 'hi'}, context)

    assert _run(leaf) == Status.SUCCESS and context.last_failure is None   # the step did not fail: the tree recovered

    leaf = build('check_after', 'step2/check_after', {'text': 'hi'}, _context({'panel': '1 2 0'}))
    assert _run(leaf) == Status.FAILURE and leaf.outputs['failed_part']['skill'] == 'is_ready'

    timed, _ = _trees(tmp_path / 'timed', {'wait': _xml(f'<Sequence>{recovered}<Timeout msec="20"><Sleep msec="5000"/>'
                                                         '</Timeout></Sequence>')}, fails=['navigate_to'])
    leaf     = timed('wait', 'step3/wait', {'text': 'hi'}, _context({'panel': '1 2 0'}))
    assert _run(leaf) == Status.FAILURE and 'no part of it failed' in leaf.message     # a Timeout ended it, later


def test_a_tree_inside_itself_too_deep_or_with_no_file_fails_at_once(tmp_path):
    build, runner = _trees(tmp_path, {'loop_a': _xml('<Action ID="loop_b" text="x"/>'), 'loop_b': _xml('<loop_a text="x"/>')})
    leaf          = build('loop_a', 'step1/loop_a', {'text': 'x'}, _context())

    assert _run(leaf) == Status.FAILURE and 'cannot call itself' in leaf.message and runner.calls == []

    nested, runner = _trees(tmp_path / 'deep', {'t1': '<root BTCPP_format="4"><BehaviorTree ID="t1"><t2 text="x"/>'
                                                       '</BehaviorTree></root>',       # its tree ID is its own name
                                                'tb': _xml('<t2 text="x"/>'), 't2': _xml('<t3 text="x"/>'),
                                                't3': _xml('<t4 text="x"/>'), 't4': _xml('<say text="x"/>')})
    leaf = nested('t2', 'step1/t2', {'text': 'x'}, _context())
    assert _run(leaf) == Status.SUCCESS and runner.calls[0][0] == 'say'     # 3 trees in each other: the limit
    leaf = nested('t1', 'step2/t1', {'text': 'x'}, _context())
    assert _run(leaf) == Status.FAILURE and 'too deep' in leaf.message and len(runner.calls) == 1   # 4

    bad, _ = _trees(tmp_path / 'bad', {'retry': _xml('<RetryUntilSuccessful num_attempts="x"><say text="x"/></RetryUntilSuccessful>')})
    leaf   = bad('retry', 'step3/retry', {'text': 'x'}, _context())
    assert _run(leaf) == Status.FAILURE and 'does not build' in leaf.message

    (tmp_path / 'loop_b' / 'tree.xml').unlink()
    leaf = build('loop_b', 'step2/loop_b', {'text': 'x'}, _context())
    assert _run(leaf) == Status.FAILURE and 'no tree file' in leaf.message
