import threading

from pathlib import Path

import pytest

from skill_registry import Registry

from orchestrator.planning.planner import Planner, load_tree_library
from orchestrator.planning.backends import BackendError
from orchestrator.planning.external import ExternalPlannerBackend


CONTRACTS = Path(__file__).resolve().parents[2] / 'g1_skills' / 'g1_skills'
TREES     = Path(__file__).resolve().parents[2] / 'orchestrator' / 'test_trees'
PLACES    = {'inside_freight_elevator_button_panel': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}


def _session(
    backend: ExternalPlannerBackend,
    requests: list,
    answers: list[str],
) -> threading.Thread:
    """A fake session: answers each open request with the next text, as planner_cli would."""
    def run():
        answered = set()
        while answers:
            open_calls = [request for request in list(requests) if request['state'] == 'open' and request['id'] not in answered]
            if open_calls:
                answered.add(open_calls[-1]['id'])
                backend.deliver({'id': 'stale', 'text': 'an answer to another call'})
                backend.deliver({'id': open_calls[-1]['id'], 'text': answers.pop(0)})

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    return thread


def test_reply_is_the_answer_to_this_call():
    requests = []
    backend  = ExternalPlannerBackend(requests.append, timeout_s=5.0)
    _session(backend, requests, ['I use the floor 1 press tree.\n<use_tree id="freight_elevator_floor1_press_agentic"/>'])
    reply    = backend.complete('the system prompt', [{'role': 'user', 'content': 'Task: call the elevator'}])

    assert reply.text.endswith('<use_tree id="freight_elevator_floor1_press_agentic"/>')
    assert reply.assistant == {'role': 'assistant', 'content': reply.text}
    assert [request['state'] for request in requests] == ['open', 'closed']
    assert requests[0]['system'] == 'the system prompt'


def test_no_answer_times_out_and_closes_the_call():
    requests = []
    backend  = ExternalPlannerBackend(requests.append, timeout_s=0.5)

    with pytest.raises(BackendError, match='no answer from the session'):
        backend.complete('system', [{'role': 'user', 'content': 'Task: x'}])

    assert requests[-1]['state'] == 'closed'


def test_planner_with_a_session_that_reuses_a_tree():
    requests = []
    backend  = ExternalPlannerBackend(requests.append, timeout_s=5.0)
    planner  = Planner(backend, Registry.from_directory(CONTRACTS), library=load_tree_library(TREES))
    _session(backend, requests, ['<use_tree id="lobby_tour"/>', 'Fixed.\n<use_tree id="freight_elevator_floor1_press_agentic"/>'])
    result   = planner.plan('press floor 1', PLACES)

    assert result.ok
    assert result.reused == 'freight_elevator_floor1_press_agentic'
    assert result.attempts == 2
    assert 'There is no existing tree "lobby_tour"' in requests[2]['messages'][-1]['content']
