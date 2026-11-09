from pathlib import Path

from skill_registry import Registry

from orchestrator.planner import Planner, build_system_prompt
from orchestrator.llm_backends import BackendError, Reply, ScriptedBackend


CONTRACTS = Path(__file__).resolve().parents[2] / 'skill_registry' / 'contracts'
TREES     = Path(__file__).resolve().parents[2] / 'skill_registry' / 'trees'
PLACES    = {'call_panel': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}

BAD_TREE  = '<root BTCPP_format="4"><BehaviorTree ID="t"><Sequence><navigate_to goal="{call_panel}"/><door_open/></Sequence></BehaviorTree></root>'
GOOD_TREE = (TREES / 'call_elevator.xml').read_text()


def _planner(replies: list[str], events: list | None = None) -> tuple[Planner, ScriptedBackend]:
    backend = ScriptedBackend(replies)
    planner = Planner(
        backend,
        Registry.from_directory(CONTRACTS),
        on_event=lambda kind, text, **data: events.append((kind, text)) if events is not None else None
    )

    return planner, backend


def test_valid_tree_on_first_attempt():
    planner, _ = _planner([f'I walk to the panel.\n<tree>{GOOD_TREE}</tree>'])
    result     = planner.plan('call the elevator', PLACES, blind=True)

    assert result.ok
    assert result.attempts == 1
    assert 'call_elevator' in result.tree


def test_validator_errors_go_back_to_the_model():
    events          = []
    planner, backend = _planner([f'<tree>{BAD_TREE}</tree>', f'Fixed.<tree>{GOOD_TREE}</tree>'], events)
    result          = planner.plan('call the elevator', PLACES)

    assert result.ok
    assert result.attempts == 2
    assert 'door_open' in backend.calls[1]
    assert ('plan', 'Fixed.') in events


def test_gives_up_after_max_attempts():
    planner, _ = _planner(['I cannot.'])
    result     = planner.plan('fly', PLACES)

    assert not result.ok
    assert result.attempts == planner.max_attempts


def test_live_skills_are_enforced():
    planner, backend = _planner([f'<tree>{GOOD_TREE}</tree>'])
    result           = planner.plan('call the elevator', PLACES, live_skills={'navigate_to', 'say'})

    assert not result.ok
    assert 'press_button' in backend.calls[1]


def test_backend_error_stops_planning():
    class Broken:
        name = 'broken'

        def complete(self, system, messages) -> Reply:
            raise BackendError('no connection to the API')

    planner = Planner(Broken(), Registry.from_directory(CONTRACTS))
    result  = planner.plan('call the elevator')

    assert not result.ok
    assert result.message == 'no connection to the API'


def test_system_prompt_is_stable_and_lists_skills():
    registry = Registry.from_directory(CONTRACTS)
    prompt   = build_system_prompt(registry, {'nuc', 'orin'})

    assert prompt == build_system_prompt(registry, {'orin', 'nuc'})
    assert 'press_button' in prompt
    assert 'thor' in prompt


def test_unknown_place_goes_back_to_the_model():
    planner, backend = _planner([f'<tree>{GOOD_TREE.replace("{call_panel}", "{lobby}")}</tree>', f'<tree>{GOOD_TREE}</tree>'])
    result           = planner.plan('call the elevator', PLACES, blind=True)

    assert result.ok
    assert 'lobby' in backend.calls[1]
