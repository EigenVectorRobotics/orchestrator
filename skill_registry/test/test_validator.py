from pathlib import Path

import pytest

from skill_registry import ContractError, Registry, TreeValidator, ValidatorConfig, load_tree, parse_contract


PACKAGE   = Path(__file__).resolve().parent.parent
CONTRACTS = PACKAGE / 'contracts'


def _tree(body: str) -> str:
    return f'<root BTCPP_format="4"><BehaviorTree ID="main">{body}</BehaviorTree></root>'


def _errors(body: str, **config) -> list[str]:
    validator = TreeValidator(Registry.from_directory(CONTRACTS), ValidatorConfig(**config))

    return [issue.message for issue in validator.validate(_tree(body)) if issue.level == 'error']


def test_seed_contracts_load():
    registry = Registry.from_directory(CONTRACTS)

    assert 'navigate_to' in registry
    assert len(registry) == len(list(CONTRACTS.glob('*.yaml')))


def test_the_library_tree_is_valid_online_only():
    registry = Registry.from_directory(CONTRACTS)
    tree     = load_tree(PACKAGE / 'trees' / 'freight_elevator_go_to_place.xml')

    assert TreeValidator(registry).is_valid(tree)
    assert not TreeValidator(registry, ValidatorConfig(blind=True)).is_valid(tree)


def test_unknown_skill():
    assert _errors('<fly_to/>') == ['unknown skill fly_to']


def test_missing_and_unknown_ports():
    errors = _errors('<navigate_to speed="2"/>')

    assert 'navigate_to: missing input goal' in errors
    assert 'navigate_to: unknown port speed' in errors


def test_enum_literal_checked_but_blackboard_passes():
    assert _errors('<set_profile profile="fast"/>')
    assert not _errors('<set_profile profile="{profile}"/>')


def test_thor_skill_needs_fallback():
    assert _errors('<ask_vlm question="q" choices="a;b"/>')
    assert _errors('<Fallback><AlwaysSuccess/><ask_vlm question="q" choices="a;b"/></Fallback>')                 # last child: no recovery after it
    assert not _errors('<Fallback><ask_vlm question="q" choices="a;b"/><AlwaysSuccess/></Fallback>')


def test_thor_skill_is_fine_when_thor_is_essential():
    assert not _errors('<ask_vlm question="q" choices="a;b"/>', essential_devices={'nuc', 'orin', 'thor'})


def test_disabled_skill_is_refused():
    assert 'door_open_vlm is disabled' in _errors('<Fallback><door_open_vlm/><AlwaysSuccess/></Fallback>')


def test_door_open_is_a_nuc_watch():
    assert not _errors('<door_open/>')


def test_parallel_resource_conflict():
    errors = _errors('<Parallel><button_in_view query="button"/><press_button target_description="button"/></Parallel>')

    assert any('resource tracker' in error for error in errors)


def test_parallel_without_conflict():
    assert not _errors('<Parallel><navigate_to goal="{goal}"/><say text="hello"/></Parallel>')


def test_blind_rejects_cloud_skill():
    registry = Registry.from_directory(CONTRACTS)
    registry.add(
        parse_contract({
            'name':        'plan_with_claude',
            'kind':        'action',
            'device':      'nuc',
            'interface':   {'transport': 'service', 'name': '/planner/plan', 'ros_type': 'std_srvs/srv/Trigger'},
            'timeout_s':   30,
            'needs_cloud': True,
        })
    )
    validator = TreeValidator(registry, ValidatorConfig(blind=True))

    assert not validator.is_valid(_tree('<plan_with_claude/>'))


def test_liveness():
    assert _errors('<say text="hi"/>', live_skills={'navigate_to'}) == ['say is not live now']


def test_condition_must_only_read():
    with pytest.raises(ContractError):
        parse_contract({
            'name':      'bad',
            'kind':      'condition',
            'device':    'nuc',
            'interface': {'transport': 'topic', 'name': '/x'},
        })


def test_enum_choices_come_from_values():
    contract = parse_contract({
        'name':      'pick',
        'kind':      'action',
        'device':    'nuc',
        'interface': {'transport': 'service', 'name': '/pick', 'ros_type': 'std_srvs/srv/Trigger'},
        'timeout_s': 1,
        'inputs':    [{'name': 'mode', 'type': 'enum', 'values': {'slow': 0, 'fast': 1}}],
    })

    assert contract.input('mode').choices == ['slow', 'fast']


def test_announce_adds_new_skill_and_expires():
    registry = Registry.from_directory(CONTRACTS)
    raw      = {
        'name':      'open_door',
        'kind':      'composite',
        'device':    'nuc',
        'interface': {'transport': 'tree', 'name': 'open_door'},
    }

    registry.announce(raw, now=100.0)

    assert 'open_door' in registry
    assert [contract.name for contract in registry.live(max_age_s=5, now=103.0)] == ['open_door']
    assert registry.live(max_age_s=5, now=110.0) == []


def test_number_attributes_must_be_integers():
    assert _errors('<Timeout msec="5000.0"><say text="hi"/></Timeout>') == ['Timeout msec must be an integer, not 5000.0']
    assert not _errors('<RetryUntilSuccessful num_attempts="-1"><say text="hi"/></RetryUntilSuccessful>')


def test_subtree_is_judged_where_it_is_called():
    called_in_fallback = (
        '<root BTCPP_format="4">'
        '<BehaviorTree ID="main"><Fallback><SubTree ID="check"/><say text="help"/></Fallback></BehaviorTree>'
        '<BehaviorTree ID="check"><ask_vlm question="q" choices="a;b"/></BehaviorTree>'
        '</root>'
    )
    called_bare = called_in_fallback.replace('<Fallback><SubTree ID="check"/><say text="help"/></Fallback>', '<SubTree ID="check"/>')
    validator   = TreeValidator(Registry.from_directory(CONTRACTS))

    assert validator.is_valid(called_in_fallback)
    assert not validator.is_valid(called_bare)
    assert not validator.is_valid(called_in_fallback.replace('<ask_vlm question="q" choices="a;b"/>', '<SubTree ID="check"/>'))


def test_empty_contracts_directory_is_an_error(tmp_path):
    with pytest.raises(ContractError, match='no contracts'):
        Registry.from_directory(tmp_path)


def test_vlm_question_fields_must_be_inputs():
    raw = {
        'name':      'lit',
        'kind':      'condition',
        'device':    'thor',
        'interface': {'transport': 'vlm', 'question': 'Is the {target} lit?', 'choices': ['yes', 'no']},
    }

    with pytest.raises(ContractError, match='target'):
        parse_contract(raw)

    assert parse_contract({**raw, 'inputs': [{'name': 'target', 'type': 'string'}]})


def test_blackboard_keys_must_be_places_or_written():
    places = {'call_panel'}

    assert not _errors('<navigate_to goal="{call_panel}"/>', known_keys=places)
    assert _errors('<navigate_to goal="{nowhere}"/>', known_keys=places) == [
        'navigate_to: goal={nowhere} reads a key that is not a named place and that the tree never writes'
    ]
    assert not _errors('<Sequence><SetBlackboard value="1 2 0" output_key="spot"/><navigate_to goal="{spot}"/></Sequence>', known_keys=places)
    assert not _errors('<Sequence><ask_vlm question="where?" choices="a;b" answer="{where}"/><say text="{where}"/></Sequence>',
                       known_keys=places, essential_devices={'nuc', 'orin', 'thor'})
    assert not _errors('<navigate_to goal="{nowhere}"/>')                                  # no known_keys: not checked


def test_no_walking_while_an_arm_moves():
    errors = _errors('<Parallel><navigate_to goal="{goal}"/><press_button target_description="elevator up call button"/></Parallel>')

    assert any('resource base' in error for error in errors)


def test_failure_when_reads_action_feedback_only():
    raw = {
        'name':      'go',
        'kind':      'action',
        'device':    'nuc',
        'timeout_s': 10,
        'interface': {'transport': 'topic', 'name': '/goal', 'ros_type': 'std_msgs/msg/String',
                      'failure_when': {'field': 'state', 'equals': 'blocked', 'hold_s': 2}},
    }

    with pytest.raises(ContractError, match='failure_when'):
        parse_contract(raw)

    held = Registry.from_directory(CONTRACTS).get('navigate_to').interface.failure_when

    assert (held.field, held.equals, held.hold_s) == ('state', 'blocked', 10)


def test_the_stance_checks_need_their_topics_and_only_conditions_record():
    base = {'name': 'x', 'kind': 'condition', 'device': 'nuc'}

    view = {'transport': 'in_view', 'name': '/keyboard_input', 'ros_type': 'std_msgs/msg/String',
            'success_when': {'topic': '/track_3d/is_tracking', 'ros_type': 'std_msgs/msg/Bool', 'field': 'data', 'equals': True},
            'target': {'box_topic': '/b', 'pose_topic': '/p'}}
    seek = {**base, 'kind': 'action', 'timeout_s': 15, 'inputs': [{'name': 'query', 'type': 'string'}]}

    with pytest.raises(ContractError, match='a condition must only read'):
        parse_contract({**base, 'interface': view})

    with pytest.raises(ContractError, match='target.pose_topic and a query input'):
        parse_contract({**seek, 'interface': {**view, 'target': {'box_topic': '/b'}}})

    assert parse_contract({**seek, 'interface': view}).interface.target['pose_topic'] == '/p'

    with pytest.raises(ContractError, match='record is for conditions'):
        parse_contract({**base, 'kind': 'action', 'timeout_s': 1, 'record': True,
                        'interface': {'transport': 'topic', 'name': '/t', 'ros_type': 'std_msgs/msg/Empty'}})

    assert parse_contract({**base, 'record': True, 'interface': {'transport': 'reach'}}).record


def test_a_watch_equals_input_names_an_input():
    base   = {'name': 'x', 'kind': 'condition', 'device': 'nuc'}

    watch = {'transport': 'watch', 'success_when': {'topic': '/f', 'ros_type': 'std_msgs/msg/String', 'equals_input': 'floor'}}

    with pytest.raises(ContractError, match='equals_input names no input'):
        parse_contract({**base, 'interface': watch})

    assert parse_contract({**base, 'interface': watch, 'inputs': [{'name': 'floor', 'type': 'string'}]})

    old = {'transport': 'watch', 'success_when': {'topic': '/f', 'ros_type': 'std_msgs/msg/String', 'equals': '{floor}'}}

    with pytest.raises(ContractError, match='use equals_input: floor'):
        parse_contract({**base, 'interface': old, 'inputs': [{'name': 'floor', 'type': 'string'}]})


def test_alive_needs_something_to_check():
    with pytest.raises(ContractError, match='an alive skill needs'):
        parse_contract({'name': 'x_up', 'kind': 'condition', 'device': 'nuc', 'interface': {'transport': 'alive', 'alive': {}}})


def test_command_needs_a_command():
    with pytest.raises(ContractError, match='a command skill needs'):
        parse_contract({'name': 'start_x', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5, 'interface': {'transport': 'command', 'command': ' '}})


def test_preflight_tree_is_valid():
    validator = TreeValidator(Registry.from_directory(CONTRACTS), ValidatorConfig())
    issues    = validator.validate((PACKAGE / 'preflight' / 'preflight.xml').read_text())

    assert [issue.message for issue in issues if issue.level == 'error'] == []


def test_a_setblackboard_copy_from_a_missing_place_is_refused():
    copy = '<Sequence><SetBlackboard value="{target_floor_1}" output_key="goal"/><navigate_to goal="{goal}"/></Sequence>'

    assert any('value={target_floor_1} reads a key that is not a named place' in error
               for error in _errors(copy, known_keys={'call_panel'}))
    assert not _errors(copy, known_keys={'target_floor_1'})
    assert not _errors(copy)                                                # no places given: keys are not checked
    written = '<Sequence><SetBlackboard value="2" output_key="floor"/><SetBlackboard value="{floor}" output_key="goal_floor"/></Sequence>'
    assert not _errors(written, known_keys=set())                           # a key the tree writes itself
