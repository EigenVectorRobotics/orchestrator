"""agent_spec is the one place the executor and the bridge share: the executor checks what the schema allows, and each
call carries the protocol, its fields and the limits the executor enforces."""

import inspect

from pathlib import Path

from skill_registry import Registry

from orchestrator import agent_spec as spec
from orchestrator.agent import AgentSession, offset_in_force
from orchestrator.agent_push import open_request


CONTRACTS = Path(__file__).resolve().parents[2] / 'skill_registry' / 'contracts'


def test_every_phase_belongs_to_an_agent_skill():
    registry = Registry.from_directory(CONTRACTS)

    for phase, skill in spec.PHASE_SKILL.items():
        assert registry.get(skill).interface.transport == 'agent', phase
        assert phase in spec.PHASE_DECISIONS


def test_the_schema_holds_every_field_decide_takes():
    taken = set(inspect.signature(AgentSession._tool_decide).parameters) - {'self'}

    assert taken == set(spec.DECISION_FIELDS)
    assert set(spec.schema('manipulation_recovery', ('retry',))['properties']) == taken


def test_a_call_carries_the_protocol_its_fields_and_the_limits():
    inputs  = {'target_description': 'floor 1 button', 'base_goal': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}}
    history = [{'skill': 'set_press_offset', 'inputs': {'x': 0.01, 'y': 0.0, 'z': 0.0}}]
    request = open_request('manipulation_recovery', 'diagnose', 'task text', {'history': history}, inputs)

    assert request['protocol'] == spec.PROTOCOL and request['phase'] == 'diagnose'
    assert request['button'] == 'floor 1 button' and request['base_goal'] == inputs['base_goal']
    assert request['limits'] == spec.limits()
    assert request['offset_in_force'] == [0.01, 0.0, 0.0]


def test_the_offset_in_force_is_the_press_s_own_when_it_is_known():
    history = [{'skill': 'set_press_offset', 'inputs': {'x': 0.01, 'y': 0.0, 'z': 0.0}},
               {'skill': 'press_judge', 'decision': 'pressed'}]

    assert offset_in_force(history) == [0.0, 0.0, 0.0]                 # set before the last press judged pressed
    assert offset_in_force(history, [0.002, -0.001, 0.0]) == [0.002, -0.001, 0.0]
