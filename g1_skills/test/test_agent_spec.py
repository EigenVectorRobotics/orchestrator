"""Each agent skill's spec.py is the one place the executor and the bridge share: the executor checks what the schema
allows, and each call carries the protocol, its fields and the limits the executor enforces."""

from pathlib import Path

from skill_registry import Registry

from orchestrator.agent import protocol
from orchestrator.agent.api import AgentCall
from orchestrator.agent.api import Agent, AgentSession
from orchestrator.agent.link import open_request
from orchestrator.tree.engine import Context

from g1_skills.lib.press import offset_in_force


CONTRACTS = Path(__file__).resolve().parents[1] / 'g1_skills'
REGISTRY  = Registry.from_directory(CONTRACTS)


def test_every_agent_contract_has_a_spec_and_every_spec_a_contract():
    agents = {contract.name for contract in REGISTRY if contract.interface.transport == 'agent'}

    assert set(protocol.agent_skills()) == agents == {'manipulation_recovery', 'press_judge', 'operator_confirm'}

    for name, spec in protocol.agent_skills().items():
        assert set(spec.GOING_ON) <= {decision for decisions in spec.PHASES.values() for decision in decisions}, name
        assert spec.ANSWERED_BY in ('model', 'operator'), name
        assert set(REGISTRY.get(name).input('phase').choices) == set(spec.PHASES), name


def test_the_schema_holds_every_field_a_decision_may_carry():
    spec    = protocol.load_spec('manipulation_recovery')
    schema  = protocol.schema(spec.FIELDS, ('retry',))
    session = _session('diagnose', {})

    assert set(schema['properties']) == {'decision', 'reason', *spec.FIELDS}
    assert session.decide({'decision': 'retry', 'reason': 'r', 'speed': 1.0}).is_error         # a field no spec has


def test_a_call_carries_the_protocol_its_fields_and_the_limits():
    inputs  = {'target_description': 'floor 1 button', 'base_goal': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}}
    history = [{'skill': 'set_press_offset', 'inputs': {'x': 0.01, 'y': 0.0, 'z': 0.0}}]
    request = open_request(_session('diagnose', inputs, {'history': history}), 'task text')

    assert request['protocol'] == protocol.PROTOCOL and request['phase'] == 'diagnose'
    assert request['button'] == 'floor 1 button' and request['base_goal'] == inputs['base_goal']
    assert request['limits'] == protocol.load_spec('manipulation_recovery').limits()
    assert request['offset_in_force'] == [0.01, 0.0, 0.0]


def test_the_offset_in_force_is_the_press_s_own_when_it_is_known():
    history = [{'skill': 'set_press_offset', 'inputs': {'x': 0.01, 'y': 0.0, 'z': 0.0}},
               {'skill': 'press_judge', 'decision': 'pressed'}]

    assert offset_in_force(history) == [0.0, 0.0, 0.0]                 # set before the last press judged pressed
    assert offset_in_force(history, [0.002, -0.001, 0.0]) == [0.002, -0.001, 0.0]


def _session(phase: str, inputs: dict, evidence: dict | None = None) -> AgentSession:
    contract = REGISTRY.get('manipulation_recovery')
    evidence = {'last_failure': None, 'history': [], **(evidence or {})}

    return AgentSession(Agent(None).skill(contract), AgentCall(contract, inputs, Context(), phase), evidence, lambda *a, **k: None)
