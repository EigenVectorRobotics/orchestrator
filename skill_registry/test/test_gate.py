"""The gate list: which calls inside a code skill still ask the operator"""

from skill_registry import GateList, parse_contract
from skill_registry.deny import PatternList


def _contract(kind='action', **interface):
    raw = {'name': 'skill', 'kind': kind, 'device': 'nuc', 'description': 'x',
           'interface': {'ros_type': 'a/msg/B', **interface}}

    return parse_contract(raw if kind == 'condition' else raw | {'timeout_s': 5})


def test_anything_that_can_act_asks_even_when_nobody_listed_it():
    gate = GateList.load()

    assert gate.check(_contract(transport='action', name='/route_planner/navigate_to_goal'))
    assert gate.check(_contract(transport='service', name='/unitree_interface/change_mode'))
    assert gate.check(_contract(transport='topic', name='/a_topic_no_one_thought_of'))


def test_a_call_that_only_reads_never_asks():
    gate = GateList.load()

    assert gate.check(_contract(kind='condition', transport='watch',
                                success_when={'topic': '/s', 'ros_type': 'a/msg/B', 'field': 'ok', 'equals': True})) == []


def test_the_operator_can_make_a_harmless_skill_quiet():
    assert GateList.load().check(_contract(transport='topic', name='/speak')) == []
    assert GateList(quiet=PatternList(names=['/beep'])).check(_contract(transport='topic', name='/speak'))
