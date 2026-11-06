import pytest

from skill_registry import ContractError, DenyList, Registry, parse_contract


def _contract(skill: str = 'probe', **interface) -> dict:
    return {'name': skill, 'kind': 'action', 'device': 'nuc', 'timeout_s': 5,
            'interface': {'transport': 'topic', 'name': '/probe', 'ros_type': 'std_msgs/msg/String', **interface}}


def _reasons(raw: dict) -> list[str]:
    return DenyList.load().check(parse_contract(raw))


def test_low_level_names_and_types_are_denied():
    assert _reasons(_contract()) == []
    assert _reasons(_contract(name='rt/lowcmd'))
    assert _reasons(_contract(name='/arm_cmd', ros_type='unitree_hg/msg/LowCmd'))
    assert _reasons(_contract(transport='service', name='unitree_interface/ready_locomotion', ros_type='std_srvs/srv/Trigger'))


def test_a_watched_topic_is_checked_too():
    watch = {'transport': 'watch', 'success_when': {'topic': '/rt/lowstate', 'ros_type': 'std_msgs/msg/Bool'}}

    assert _reasons({'name': 'probe', 'kind': 'condition', 'device': 'nuc', 'interface': watch})


def test_change_mode_may_send_only_idle_or_high_level():
    service = {'transport': 'service', 'name': '/unitree_interface/change_mode',
               'ros_type': 'unitree_interface_msgs/srv/ChangeControlMode'}
    mode    = lambda values: [{'name': 'mode', 'type': 'enum', 'field': 'requested_mode', 'values': values}]

    assert _reasons(_contract() | {'interface': service, 'inputs': mode({'idle': 1, 'high_level': 2})}) == []
    assert _reasons(_contract() | {'interface': service, 'inputs': mode({'idle': 1, 'low_level': 4})})
    assert _reasons(_contract() | {'interface': service, 'inputs': [{'name': 'requested_mode', 'type': 'int'}]})
    assert _reasons(_contract() | {'interface': service})                                       # unset: MONOSTATE 0
    assert _reasons(_contract() | {'interface': service | {'fixed': {'requested_mode': 3}}})


def test_the_registry_keeps_a_denied_contract_out():
    registry = Registry([parse_contract(_contract()), parse_contract(_contract('bad', name='rt/arm_sdk'))])

    assert 'probe' in registry and 'bad' not in registry and 'rt/arm_sdk' in registry.denied['bad'][0]

    with pytest.raises(ContractError, match='denied'):
        Registry().announce(_contract('bad2', name='/lowcmd'))


def test_a_skill_that_needs_a_denied_skill_is_left_out_too(tmp_path):
    import yaml

    (tmp_path / 'bad.yaml').write_text(yaml.safe_dump(_contract('bad', name='rt/lowcmd')))
    (tmp_path / 'uses.yaml').write_text(yaml.safe_dump(_contract('uses') | {'fallback': ['bad']}))
    (tmp_path / 'fine.yaml').write_text(yaml.safe_dump(_contract('fine')))

    registry = Registry.from_directory(tmp_path)

    assert list(registry) == [registry.get('fine')] and registry.denied['uses'] == ['it needs the denied skill bad']
