from pathlib import Path

import pytest

from skill_registry import ContractError, Registry, TreeValidator, ValidatorConfig, parse_contract


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


def test_example_tree_is_valid_blind():
    registry  = Registry.from_directory(CONTRACTS)
    validator = TreeValidator(registry, ValidatorConfig(blind=True))

    assert validator.is_valid((PACKAGE / 'trees' / 'call_elevator.xml').read_text())


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
    assert _errors('<door_open/>')
    assert _errors('<Fallback><AlwaysSuccess/><door_open/></Fallback>')                  # last child: no recovery after it
    assert not _errors('<Fallback><door_open/><AlwaysSuccess/></Fallback>')


def test_thor_skill_is_fine_when_thor_is_essential():
    assert not _errors('<door_open/>', essential_devices={'nuc', 'orin', 'thor'})


def test_parallel_resource_conflict():
    errors = _errors('<Parallel><track query="button"/><press_button target_description="button"/></Parallel>')

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
        '<BehaviorTree ID="check"><door_open/></BehaviorTree>'
        '</root>'
    )
    called_bare = called_in_fallback.replace('<Fallback><SubTree ID="check"/><say text="help"/></Fallback>', '<SubTree ID="check"/>')
    validator   = TreeValidator(Registry.from_directory(CONTRACTS))

    assert validator.is_valid(called_in_fallback)
    assert not validator.is_valid(called_bare)
    assert not validator.is_valid(called_in_fallback.replace('<door_open/>', '<SubTree ID="check"/>'))


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


def test_a_compare_question_may_use_only_inputs():
    raw = {
        'name':      'lit',
        'kind':      'condition',
        'device':    'thor',
        'interface': {'transport': 'vlm', 'question': 'Is the {target_description} lit?', 'frames': 'press_capture',
                      'compare_question': 'Is the {button} lit in the right photo?', 'choices': ['lit', 'not_lit'],
                      'success_answer': 'lit'},
        'inputs':    [{'name': 'target_description', 'type': 'string'}],
    }

    with pytest.raises(ContractError, match='button'):
        parse_contract(raw)


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

    with pytest.raises(ContractError, match='box_topic'):
        parse_contract({**base, 'interface': {'transport': 'in_view'}})

    with pytest.raises(ContractError, match='pose_topic and target.frame'):
        parse_contract({**base, 'interface': {'transport': 'in_reach', 'target': {'pose_topic': '/p'}}})

    with pytest.raises(ContractError, match='record is for conditions'):
        parse_contract({**base, 'kind': 'action', 'timeout_s': 1, 'record': True,
                        'interface': {'transport': 'topic', 'name': '/t', 'ros_type': 'std_msgs/msg/Empty'}})

    assert parse_contract({**base, 'record': True, 'interface': {'transport': 'in_view', 'target': {'box_topic': '/b'}}}).record


def test_a_points_in_box_check_names_its_pose_input_and_a_watch_its_equals_input():
    base   = {'name': 'x', 'kind': 'condition', 'device': 'nuc'}
    target = {'cloud_topic': '/c', 'frame': 'map', 'robot_frame': 'base_link', 'box': 'door'}

    with pytest.raises(ContractError, match='cloud_topic, target.frame and target.robot_frame'):
        parse_contract({**base, 'interface': {'transport': 'points_in_box', 'target': {'cloud_topic': '/c'}}})

    with pytest.raises(ContractError, match='target.box must name a pose input'):
        parse_contract({**base, 'interface': {'transport': 'points_in_box', 'target': target},
                        'inputs': [{'name': 'door', 'type': 'string'}]})

    assert parse_contract({**base, 'interface': {'transport': 'points_in_box', 'target': target},
                           'inputs': [{'name': 'door', 'type': 'pose'}]})

    watch = {'transport': 'watch', 'success_when': {'topic': '/f', 'ros_type': 'std_msgs/msg/String', 'equals': '{floor}'}}

    with pytest.raises(ContractError, match='equals names no input'):
        parse_contract({**base, 'interface': watch})

    assert parse_contract({**base, 'interface': watch, 'inputs': [{'name': 'floor', 'type': 'string'}]})
