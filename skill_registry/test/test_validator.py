from pathlib import Path

import pytest

from skill_registry import ContractError, Registry, TreeValidator, ValidatorConfig, parse_contract


CONTRACTS = Path(__file__).resolve().parent / 'contracts'      # a made-up robot


def _tree(body: str) -> str:
    return f'<root BTCPP_format="4"><BehaviorTree ID="main">{body}</BehaviorTree></root>'


def _errors(body: str, **config) -> list[str]:
    validator = TreeValidator(Registry.from_directory(CONTRACTS), ValidatorConfig(**config))

    return [issue.message for issue in validator.validate(_tree(body)) if issue.level == 'error']


def test_seed_contracts_load():
    registry = Registry.from_directory(CONTRACTS)

    assert 'navigate_to' in registry
    assert len(registry) + len(registry.denied) == len(list(CONTRACTS.glob('*.yaml')))
    assert 'ready_walk' in registry.denied


@pytest.mark.parametrize('body, config, says', [
    ('<fly_to/>',                                           {},                              ['unknown skill fly_to']),
    ('<navigate_to speed="2"/>',                            {},                              ['missing input goal', 'unknown port speed']),
    ('<set_profile profile="fast"/>',                       {},                              ['profile=fast is not one of']),
    ('<say text="hi"/>',                                    {'live_skills': {'navigate_to'}}, ['not live']),
    ('<Timeout msec="5000.0"><say text="hi"/></Timeout>',  {},                              ['must be an integer']),
], ids=['unknown_skill', 'missing_and_unknown_ports', 'enum_literal', 'not_live', 'number_not_integer'])
def test_a_bad_tree_gets_one_error_for_each_fault(body, config, says):
    errors = _errors(body, **config)

    assert len(errors) == len(says) and all(any(said in error for error in errors) for said in says), errors


@pytest.mark.parametrize('body', [
    '<set_profile profile="{profile}"/>',                                                   # a blackboard value is not checked
    '<RetryUntilSuccessful num_attempts="-1"><say text="hi"/></RetryUntilSuccessful>',     # -1 is an integer: retry forever
], ids=['enum_from_blackboard', 'negative_integer'])
def test_a_good_tree_gets_no_error(body):
    assert not _errors(body)


def test_thor_skill_needs_fallback():
    assert _errors('<door_open/>')
    assert _errors('<Fallback><AlwaysSuccess/><door_open/></Fallback>')                  # last child: no recovery after it
    assert not _errors('<Fallback><door_open/><AlwaysSuccess/></Fallback>')
    assert not _errors('<door_open/>', essential_devices={'nuc', 'orin', 'thor'})      # unless Thor is essential
    assert not _errors('<door_open/>', require_fallback=False)                          # or the task agent recovers


def test_parallel_resource_conflict():
    errors = _errors('<Parallel><track query="button"/><press_button target_description="button"/></Parallel>')

    assert any('resource tracker' in error for error in errors)
    assert not _errors('<Parallel><navigate_to goal="{goal}"/><say text="hello"/></Parallel>')

    errors = _errors('<Parallel><navigate_to goal="{goal}"/><press_button target_description="elevator up call button"/></Parallel>')

    assert any('resource base' in error for error in errors)                           # no walking while an arm moves


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


def test_an_empty_or_missing_contracts_directory_is_an_empty_library(tmp_path):
    assert len(Registry.from_directory(tmp_path)) == 0
    assert len(Registry.from_directory(tmp_path / 'not_yet')) == 0


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

    compare = {
        'name':      'lit',
        'kind':      'condition',
        'device':    'thor',
        'interface': {'transport': 'vlm', 'question': 'Is the {target_description} lit?', 'frames': 'press_capture',
                      'compare_question': 'Is the {button} lit in the right photo?', 'choices': ['lit', 'not_lit'],
                      'success_answer': 'lit'},
        'inputs':    [{'name': 'target_description', 'type': 'string'}],
    }

    with pytest.raises(ContractError, match='button'):                                 # a compare question too
        parse_contract(compare)


def test_blackboard_keys_must_be_places_or_written():
    places = {'call_panel'}

    assert not _errors('<navigate_to goal="{call_panel}"/>', known_keys=places)
    errors = _errors('<navigate_to goal="{nowhere}"/>', known_keys=places)

    assert len(errors) == 1 and 'nowhere' in errors[0]
    assert not _errors('<Sequence><SetBlackboard value="1 2 0" output_key="spot"/><navigate_to goal="{spot}"/></Sequence>', known_keys=places)
    assert not _errors('<Sequence><ask_vlm question="where?" choices="a;b" answer="{where}"/><say text="{where}"/></Sequence>',
                       known_keys=places, essential_devices={'nuc', 'orin', 'thor'})
    assert not _errors('<navigate_to goal="{nowhere}"/>')                                  # no known_keys: not checked


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

    assert held is not None
