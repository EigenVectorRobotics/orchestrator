import pytest
import yaml

from skill_registry import ContractError, contract_grammar, parse_contract
from skill_registry.contract import TRANSPORTS
from skill_registry.grammar import EXAMPLE


SAY = {'name': 'say_it', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5,
       'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'},
       'inputs': [{'name': 'text', 'type': 'string', 'field': 'data'}]}


def test_the_grammar_names_every_transport_and_default_and_its_example_is_a_contract():
    grammar = contract_grammar()

    assert all(transport in grammar for transport in TRANSPORTS)
    assert "field: str = 'data'" in grammar                     # left out, a watch reads data (2026-11-11 refusal)
    assert parse_contract(yaml.safe_load(EXAMPLE)).name == 'example_skill'


@pytest.mark.parametrize('raw, says', [
    ({'kind': 'query'},                                        'action | condition | composite'),
    ({'timeout': 5},                                           'has no field timeout'),
    ({'interface': {'transport': 'ros_topic'}},                'unknown transport ros_topic; use action | service'),
    ({'interface': {'transport': 'topic', 'access': 'write'}}, 'has no field access'),
    ({'interface': {'transport': 'topic', 'name': '/speak'}},  'needs the ROS name and the ros_type'),
    ({'inputs': [{'name': 'ok', 'type': 'boolean'}]},          'string | int | float | bool | pose | enum'),
    ({'inputs': 'text'},                                       'lists of ports'),
    ({'inputs': ['text']},                                     'a port is a mapping'),
    ({'inputs': [{'type': 'string'}]},                         'a port needs name'),
    ({'device': None},                                         'needs device'),               # YAML null, not 'None'
    ({'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String', 'capture': 'press'}},
                                                               'capture is a dict'),          # it reached ROS as an
])                                                                                            # AttributeError before
def test_a_refusal_names_the_fields_and_the_values_it_takes(raw, says):
    with pytest.raises(ContractError) as refused:
        parse_contract(SAY | raw)

    assert says in str(refused.value)
