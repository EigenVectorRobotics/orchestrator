from pathlib import Path

from skill_registry import Registry, load_tree

from orchestrator.planner import Planner, build_system_prompt, extract_tree, load_tree_library, summarize_tree, tree_parameters
from orchestrator.llm_backends import BackendError, Reply, ScriptedBackend


CONTRACTS = Path(__file__).resolve().parents[2] / 'skill_registry' / 'contracts'
TREES      = Path(__file__).resolve().parents[2] / 'skill_registry' / 'trees'
TEST_TREES = Path(__file__).resolve().parents[2] / 'orchestrator' / 'test_trees'
PLACES    = {'call_panel': {'x': 0.0, 'y': 0.0, 'yaw': 0.0, 'frame': 'map'}}

BAD_TREE  = '<root BTCPP_format="4"><BehaviorTree ID="t"><Sequence><navigate_to goal="{call_panel}"/><ask_vlm question="q" choices="a;b"/></Sequence></BehaviorTree></root>'
GOOD_TREE = ('<root BTCPP_format="4">\n  <!-- Walk to the call panel, press up, wait for the door. Must run blind. -->\n'
             '  <BehaviorTree ID="call_elevator">\n    <Sequence>\n      <navigate_to goal="{call_panel}"/>\n'
             '      <Fallback>\n        <press_button target_description="up button"/>\n'
             '        <say text="Please press the up button for me."/>\n      </Fallback>\n'
             '      <door_open/>\n    </Sequence>\n  </BehaviorTree>\n</root>\n')


def _library(tmp_path: Path) -> dict:
    (tmp_path / 'call_elevator.xml').write_text(GOOD_TREE)

    return load_tree_library(tmp_path)


def _planner(replies: list[str], events: list | None = None, library: dict | None = None) -> tuple[Planner, ScriptedBackend]:
    backend = ScriptedBackend(replies)
    planner = Planner(
        backend,
        Registry.from_directory(CONTRACTS),
        on_event=lambda kind, text, **data: events.append((kind, text)) if events is not None else None,
        library=library
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
    assert 'ask_vlm runs on thor' in backend.calls[1]
    assert ('plan', 'Fixed.') in events


def test_gives_up_after_max_attempts():
    planner, _ = _planner(['I cannot.'])
    result     = planner.plan('fly', PLACES)

    assert not result.ok
    assert result.attempts == 3


def test_live_skills_are_enforced():
    planner, backend = _planner([f'<tree>{GOOD_TREE}</tree>'])
    result           = planner.plan('call the elevator', PLACES, live_skills={'navigate_to', 'say'})

    assert not result.ok
    assert 'press_button is not live now' in backend.calls[1]


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
    assert '## press_button (action)' in prompt
    assert 'runs on thor: can drop out' in prompt


def test_system_prompt_hides_disabled_skills():
    prompt = build_system_prompt(Registry.from_directory(CONTRACTS), {'nuc', 'orin'})

    assert '## door_open (condition)' in prompt
    assert '## door_open_vlm (condition)' not in prompt


def test_extract_tree():
    assert extract_tree('text <tree>\n<root/>\n</tree> more') == '<root/>'
    assert extract_tree('no tree') is None


def test_unknown_place_goes_back_to_the_model():
    planner, backend = _planner([f'<tree>{GOOD_TREE.replace("{call_panel}", "{lobby}")}</tree>', f'<tree>{GOOD_TREE}</tree>'])
    result           = planner.plan('call the elevator', PLACES, blind=True)

    assert result.ok
    assert 'goal={lobby} reads a key that is not a named place' in backend.calls[1]


def test_summarize_tree_takes_the_first_sentence_of_the_first_comment():
    tree_id, summary = summarize_tree(GOOD_TREE)

    assert tree_id == 'call_elevator'
    assert summary == 'Walk to the call panel, press up, wait for the door'


def test_tree_library_holds_every_tree_file():
    library = load_tree_library(TEST_TREES)

    assert set(library) == {summarize_tree(path.read_text())[0] for path in TEST_TREES.glob('*.xml')}
    assert library['freight_elevator_floor1_press_agentic'][1] == (TEST_TREES / 'freight_elevator_floor1_press_agentic.xml').read_text()


def test_system_prompt_lists_existing_trees_only_when_given(tmp_path):
    registry = Registry.from_directory(CONTRACTS)
    library  = _library(tmp_path)
    prompt   = build_system_prompt(registry, {'nuc', 'orin'}, library)

    assert '# Existing trees' in prompt
    assert '- call_elevator: Walk to the call panel, press up, wait for the door' in prompt
    assert '# Existing trees' not in build_system_prompt(registry, {'nuc', 'orin'})


def test_model_can_choose_an_existing_tree(tmp_path):
    events           = []
    library          = _library(tmp_path)
    planner, _       = _planner(['The call_elevator tree does this.\n<use_tree id="call_elevator"/>'], events, library)
    result           = planner.plan('call the elevator', PLACES, blind=True)

    assert result.ok
    assert result.attempts == 1
    assert result.reused == 'call_elevator'
    assert result.tree == GOOD_TREE
    assert ('reuse', 'existing tree call_elevator') in events


def test_unknown_existing_tree_goes_back_to_the_model(tmp_path):
    planner, backend = _planner(['<use_tree id="lobby_tour"/>', f'<tree>{GOOD_TREE}</tree>'], library=_library(tmp_path))
    result           = planner.plan('call the elevator', PLACES, blind=True)

    assert result.ok
    assert result.reused == ''
    assert 'There is no existing tree "lobby_tour"' in backend.calls[1]


def test_existing_tree_that_fails_validation_asks_for_a_new_tree(tmp_path):
    planner, backend = _planner(['<use_tree id="call_elevator"/>', 'I cannot.'], library=_library(tmp_path))
    result           = planner.plan('call the elevator', PLACES, live_skills={'navigate_to', 'say'})

    assert not result.ok
    assert 'The existing tree call_elevator does not pass the validator now' in backend.calls[1]


def test_speech_left_waits_for_the_last_sentence():
    from orchestrator.planner import SPEECH_CHARS_PER_S, SPEECH_GAP_S, speech_left_s

    text = 'x' * 26

    assert speech_left_s(text, 100.0, 100.0) == 26 / SPEECH_CHARS_PER_S + SPEECH_GAP_S
    assert speech_left_s(text, 100.0, 105.0) == 0.0
    assert speech_left_s('', 0.0, 100.0) == 0.0


RIDE = load_tree(TREES / 'freight_elevator_go_to_place.xml')


def test_system_prompt_lists_a_tree_with_its_parameters():
    prompt = build_system_prompt(Registry.from_directory(CONTRACTS), {'nuc', 'orin'}, load_tree_library(TREES))

    assert '- freight_elevator_go_to_place(goal="{target_floor_2}", goal_floor="2"): ' in prompt


def test_use_tree_overrides_parameters_and_keeps_the_others():
    events     = []
    planner, _ = _planner(['Floor 1.\n<use_tree id="freight_elevator_go_to_place" goal="{target_floor_1}" goal_floor="1"/>'], events, load_tree_library(TREES))
    result     = planner.plan('go to floor 1', PLACES)

    assert result.reused == 'freight_elevator_go_to_place'
    assert tree_parameters(result.tree) == {'goal': '{target_floor_1}', 'goal_floor': '1'}
    assert result.tree.replace('output_key="goal" value="{target_floor_1}"', 'output_key="goal" value="{target_floor_2}"').replace('output_key="goal_floor" value="1"', 'output_key="goal_floor" value="2"') == RIDE


def test_unknown_parameter_goes_back_to_the_model():
    planner, backend = _planner(['<use_tree id="freight_elevator_go_to_place" lobby="2"/>', 'I cannot.'], library=load_tree_library(TREES))
    planner.plan('ride to floor 2', PLACES)

    assert 'The tree "freight_elevator_go_to_place" has no parameter lobby' in backend.calls[1]


BY_HAND = ('<root BTCPP_format="4">\n  <!-- Ask a person to press the button -->\n'
           '  <BehaviorTree ID="press_by_hand">\n    <SubTree ID="ask_press"/>\n  </BehaviorTree>\n'
           '  <BehaviorTree ID="ask_press">\n    <say text="{press_ask}"/>\n  </BehaviorTree>\n</root>\n')


def test_system_prompt_lists_subtrees_with_their_keys():
    prompt = build_system_prompt(Registry.from_directory(CONTRACTS), {'nuc', 'orin'}, load_tree_library(TREES))

    assert '  - subtree walk_out (reads door_out, goal; writes exiting)' in prompt
    assert '  - subtree press_panel_button (reads press_ask, press_panel, button, press_arm_min_y, press_base_offset_x, press_base_offset_y, press_base_offset_z; ' in prompt
    assert '  - subtree stance_check (reads press_panel, button, press_arm_min_y; writes recovery_goal, recovery_walk_in, stance_reset, decision, recovery_approach, press_left_arm)' in prompt


def test_use_tree_replaces_a_subtree_with_another_tree():
    library    = {**load_tree_library(TREES), 'press_by_hand': ('Ask a person to press the button', BY_HAND)}
    planner, _ = _planner(['<use_tree id="freight_elevator_go_to_place" goal="{target_floor_1}" goal_floor="1"><replace subtree="press_panel_button" with="press_by_hand"/></use_tree>'], library=library)
    result     = planner.plan('go to floor 1', PLACES)

    assert result.reused == 'freight_elevator_go_to_place'
    assert '<BehaviorTree ID="press_panel_button">\n    <SubTree ID="ask_press"/>\n  </BehaviorTree>' in result.tree
    assert '<BehaviorTree ID="ask_press">' in result.tree and result.tree.rstrip().endswith('</root>')
    assert tree_parameters(result.tree)['goal_floor'] == '1'


def test_unknown_subtree_goes_back_to_the_model():
    planner, backend = _planner(['<use_tree id="freight_elevator_go_to_place"><replace subtree="lobby" with="call_elevator"/></use_tree>', 'I cannot.'], library=load_tree_library(TREES))
    planner.plan('ride to floor 2', PLACES)

    assert 'The tree "freight_elevator_go_to_place" has no subtree lobby.' in backend.calls[1]


FLOORS = {'call_panel': '2', 'lobby': '1'}
TWO_FLOORS = {'call_panel': {'x': 0.0, 'y': 0.0, 'z': -0.4, 'yaw': 0.0, 'frame': 'map'},
              'lobby':      {'x': 1.0, 'y': 0.0, 'z': -6.5, 'yaw': 0.0, 'frame': 'map'}}
GO_TO = ('<root BTCPP_format="4">\n  <!-- Walk to a goal place -->\n  <BehaviorTree ID="go_to">\n    <Sequence>\n'
         '      <Sequence name="parameters">\n        <SetBlackboard value="{call_panel}" output_key="goal"/>\n'
         '        <SetBlackboard value="2" output_key="goal_floor"/>\n      </Sequence>\n'
         '      <at_floor floor="{goal_floor}"/>\n      <navigate_to goal="{goal}"/>\n    </Sequence>\n  </BehaviorTree>\n</root>\n')


def test_task_message_gives_the_floor_of_each_place_and_of_the_robot():
    from orchestrator.planner import build_task_message

    message = build_task_message('go to the lobby', TWO_FLOORS, None, False, FLOORS, '2')

    assert 'Named places (on the blackboard): call_panel (floor 2), lobby (floor 1)' in message
    assert 'The robot is on floor 2.' in message
    assert 'Named places (on the blackboard): call_panel, lobby' in build_task_message('go', TWO_FLOORS, None, False)


def test_a_floor_parameter_left_out_is_set_from_its_place():
    events     = []
    planner, _ = _planner(['The lobby.\n<use_tree id="go_to" goal="{lobby}"/>'], events, {'go_to': ('Walk to a goal place', GO_TO)})
    result     = planner.plan('go to the lobby', TWO_FLOORS, floors=FLOORS, robot_floor='2')

    assert result.ok
    assert tree_parameters(result.tree) == {'goal': '{lobby}', 'goal_floor': '1'}
    assert ('parameters', "floor parameters set from their places: {'goal_floor': '1'}") in events


def test_a_floor_parameter_that_disagrees_with_its_place_goes_back_to_the_model():
    planner, backend = _planner(['<use_tree id="go_to" goal="{lobby}" goal_floor="2"/>', '<use_tree id="go_to" goal="{lobby}"/>'],
                                library={'go_to': ('Walk to a goal place', GO_TO)})
    result           = planner.plan('go to the lobby', TWO_FLOORS, floors=FLOORS)

    assert result.ok
    assert 'goal_floor="2", but {lobby} is on floor 1' in backend.calls[1]
    assert tree_parameters(result.tree)['goal_floor'] == '1'


def test_without_place_floors_the_parameters_stay_as_given():
    planner, _ = _planner(['<use_tree id="go_to" goal="{lobby}"/>'], library={'go_to': ('Walk to a goal place', GO_TO)})
    result     = planner.plan('go to the lobby', TWO_FLOORS)

    assert tree_parameters(result.tree) == {'goal': '{lobby}', 'goal_floor': '2'}


def test_planner_cli_answer_passes_parameters_to_use_tree():
    import argparse

    import pytest

    from orchestrator.planner_cli import answer_text, parse_parameters

    arguments = argparse.Namespace(use_tree='go_to', tree=None, note='To the lobby.', param=['goal={lobby}', 'goal_floor=1'])

    assert answer_text(arguments) == 'To the lobby.\n<use_tree id="go_to" goal="{lobby}" goal_floor="1"/>'

    with pytest.raises(ValueError):
        parse_parameters(['goal'])

    with pytest.raises(ValueError):
        parse_parameters(['goal="{lobby}"x=1'])
