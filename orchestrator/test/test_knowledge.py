import json

import pytest

from orchestrator.knowledge import KnowledgeError, KnowledgeStore, Sighting, wilson_lower_bound
from orchestrator.knowledge_cli import main as cli


PANEL = {'place': 'freight_elevator_call_panel'}


def seen(by='agent:press_judge', run='r1', value=None, t=100.0):
    return Sighting(by=by, time=t, run=run, evidence=['record:3'], value=value)


def ready_belief(store, count=11):
    """A value claim confirmed in two runs by two sources, then often enough to be ready for a fact"""
    claim, _ = store.note('The call panel does not light when pressed.', seen(value=False), PANEL, 'lights_when_pressed')

    for index in range(count - 1):
        store.confirm(claim.id, seen(by='operator' if index % 2 else 'check:press_reached', run=f'r{index + 2}', value=False))

    return store.get(claim.id)


def test_wilson_lower_bound_needs_many_clean_confirmations():
    assert wilson_lower_bound(3, 3) < 0.8
    assert wilson_lower_bound(11, 11) >= 0.8
    assert wilson_lower_bound(0, 0) == 0.0


def test_a_note_is_an_observation_and_a_same_key_note_confirms_it(tmp_path):
    store    = KnowledgeStore(tmp_path)
    claim, _ = store.note('The call panel does not light.', seen(value=False), PANEL, 'lights_when_pressed')
    again, change = store.note('It did not light again.', seen(run='r2', value=False), PANEL, 'lights_when_pressed')

    assert claim.id == again.id
    assert change == 'confirmed' and store.get(claim.id).tier == 'observation'     # one source only


def test_two_runs_and_two_sources_make_a_belief(tmp_path):
    store    = KnowledgeStore(tmp_path)
    claim, _ = store.note('The call panel does not light.', seen(value=False), PANEL, 'lights_when_pressed')

    store.confirm(claim.id, seen(by='operator', run='r2', value=False))

    assert store.get(claim.id).tier == 'belief'


def test_a_different_word_contradicts_and_a_fact_goes_down_to_a_belief(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path)
    claim = ready_belief(store)
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt: claim.id)
    cli(['--dir', str(tmp_path), 'approve', claim.id])

    assert store.get(claim.id).tier == 'fact'

    store.note('It lit this time.', seen(run='r99', value=True), PANEL, 'lights_when_pressed')

    assert store.get(claim.id).tier == 'belief'


def test_a_measured_number_never_contradicts_it_goes_into_the_spread(tmp_path):
    store    = KnowledgeStore(tmp_path)
    claim, _ = store.note('Offset that lit floor 1.', seen(value=[0.012, 0.0126, 0.0032]), {'place': 'cabin'}, 'press_offset')
    store.note('Offset that lit floor 1.', seen(run='r2', value=[0.010, 0.0120, 0.0030]), {'place': 'cabin'}, 'press_offset')

    stats = store.get(claim.id).value_stats()

    assert not store.get(claim.id).contradictions
    assert stats['n'] == 2 and stats['mean'][0] == pytest.approx(0.011)

    words, _ = store.note('Direction.', seen(value=['left', 'up']), PANEL, 'direction')
    flags, _ = store.note('Lights.', seen(value=True), PANEL, 'lights')

    assert store.get(words.id).value_stats() is None     # words and yes/no have no spread
    assert store.get(flags.id).value_stats() is None


def test_only_the_operator_approves_and_only_a_ready_belief(tmp_path):
    store    = KnowledgeStore(tmp_path)
    claim, _ = store.note('x', seen())

    with pytest.raises(KnowledgeError):
        store.approve(claim.id, 'operator')                 # an observation

    ready = ready_belief(store)

    with pytest.raises(KnowledgeError):
        store.approve(ready.id, 'agent:press_judge')

    fact = store.approve(ready.id, 'operator', text='The freight elevator call panel never lights.')

    assert fact.tier == 'fact' and fact.text.startswith('The freight')
    log = [json.loads(line) for line in store.log_path.read_text().splitlines()]
    assert log[-1]['action'] == 'approve' and log[-1]['edited'] is True


def test_the_cli_refuses_to_approve_without_a_terminal(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path)
    claim = ready_belief(store)
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)

    with pytest.raises(SystemExit):
        cli(['--dir', str(tmp_path), 'approve', claim.id])

    assert store.get(claim.id).tier == 'belief'


def test_a_claim_expires_after_valid_for_s(tmp_path):
    now      = [100.0]
    store    = KnowledgeStore(tmp_path, clock=lambda: now[0])
    claim, _ = store.note('A person waits in the hall.', seen(t=100.0), {'place': 'hall'}, valid_for_s=600)

    now[0] = 650.0
    assert store.expire() == []

    now[0] = 800.0
    assert store.prompt_section() == ''                     # a prompt retires expired claims first
    assert store.get(claim.id).tier == 'retired'


def test_prompt_section_has_facts_and_beliefs_not_observations(tmp_path):
    store = KnowledgeStore(tmp_path)
    store.note('Only seen once.', seen())
    belief, _ = store.note('The cabin door closes after 8 s.', seen(), {'place': 'cabin'})
    store.confirm(belief.id, seen(by='operator', run='r2'))

    text = store.prompt_section()

    assert 'closes after 8 s' in text
    assert 'Only seen once' not in text


def test_the_agent_note_tool_writes_an_observation_with_its_run(tmp_path):
    from orchestrator.agent import AgentConfig, AgentSession

    store   = KnowledgeStore(tmp_path)
    events  = []
    session = AgentSession('judge', {}, {}, None, AgentConfig(skills_dir=str(tmp_path), knowledge=store),
                           lambda kind, text, **data: events.append(kind), skill='press_judge', run_id='r7')

    output = session.call('note', {'text': 'The call panel does not light.', 'about': PANEL,
                                   'key': 'lights_when_pressed', 'value': False, 'evidence': ['frame:press']})

    claim = store.get('C-0001')

    assert not output.is_error
    assert claim.confirmations[0].by == 'agent:press_judge' and claim.confirmations[0].run == 'r7'
    assert 'knowledge' in events
    assert session.call('note', {'text': 'no evidence', 'evidence': []}).is_error


def test_many_notes_in_one_run_count_as_one_sighting(tmp_path):
    store    = KnowledgeStore(tmp_path)
    claim, _ = store.note('Panel does not light.', seen(value=False), PANEL, 'lights_when_pressed')

    for _ in range(12):
        store.note('Panel does not light.', seen(value=False), PANEL, 'lights_when_pressed')

    store.confirm(claim.id, seen(by='operator', run='r2', value=False))

    assert store.get(claim.id).tier == 'belief'
    assert not store.get(claim.id).ready_for_fact()


def test_the_agent_confirms_an_observation_by_its_id(tmp_path):
    from orchestrator.agent import AgentConfig, AgentSession

    store    = KnowledgeStore(tmp_path)
    claim, _ = store.note('The call panel does not light.', seen(by='operator', run='r1'), PANEL)
    session  = AgentSession('judge', {}, {}, None, AgentConfig(skills_dir=str(tmp_path), knowledge=store),
                            lambda kind, text, **data: None, skill='press_judge', run_id='r2')

    assert claim.id in store.prompt_section(observations=True)

    session.call('note', {'text': 'Again unlit.', 'claim': claim.id, 'evidence': ['frame:press']})

    assert store.get(claim.id).tier == 'belief'
    assert session.call('note', {'text': 'x', 'claim': 'C-9999', 'evidence': ['e']}).is_error


def test_a_claim_can_be_about_any_tags_or_none(tmp_path):
    store     = KnowledgeStore(tmp_path)
    thor, _   = store.note('The VLM on Thor is slow after 30 min.', seen(), {'device': 'thor'})
    store.note('The cabin has no network.', seen())
    door, _   = store.note('The door stays open.', seen(value=8.0), {'object': 'freight_elevator', 'place': 'cabin'}, 'open_s')
    _, change = store.note('The door stays open.', seen(run='r2', value=8.5), {'place': 'cabin ', 'object': 'freight_elevator'}, 'open_s')

    assert 'thor' in store.get(thor.id).subject()
    assert change.startswith('confirmed') and store.find({'place': 'cabin', 'object': 'freight_elevator'}, 'open_s').id == door.id

    for bad in ({'Device': 'thor'}, {'two words': 'x'}):
        with pytest.raises(KnowledgeError):
            store.note('x', seen(), bad)


def test_the_cli_lists_claims_by_tags(tmp_path, capsys):
    store = KnowledgeStore(tmp_path)
    store.note('The VLM on Thor is slow.', seen(), {'device': 'thor'})
    store.note('The door stays open.', seen(), {'object': 'freight_elevator', 'place': 'cabin'})

    cli(['--dir', str(tmp_path), 'list', '--about', 'place=cabin'])

    out = capsys.readouterr().out
    assert 'door' in out and 'Thor' not in out

    for bad in (['thor'], ['place=a', 'place=b']):
        with pytest.raises(SystemExit):
            cli(['--dir', str(tmp_path), 'list', *[arg for tag in bad for arg in ('--about', tag)]])


def test_hand_written_claims_get_clean_tags_on_load_and_a_bad_kind_still_loads(tmp_path):
    (tmp_path / 'claims.yaml').write_text('- {id: C-0001, text: Door open., about: {place: "cabin "}, key: open_s,\n'
                                          '   confirmations: [{by: operator, time: 1.0, run: r1, value: 8.0}]}\n'
                                          '- {id: C-0002, text: Slow., about: {Device: thor, skill: null},\n'
                                          '   confirmations: [{by: operator, time: 1.0, run: r1}]}\n')
    store     = KnowledgeStore(tmp_path)
    _, change = store.note('Door open.', seen(value=8.2), {'place': 'cabin'}, 'open_s')

    assert change.startswith('confirmed') and len(store.claims()) == 2
    assert store.get('C-0002').about == {'Device': 'thor'}


class FakeCode:
    """The code clock: one version number for every skill"""
    def __init__(self):
        self.version = 1

    def snapshot(self, skill):
        return {'version': self.version}

    def since(self, skill, snapshot):
        return '' if snapshot['version'] == self.version else f'nav moved to v{self.version}'


def test_a_claim_about_a_skill_earns_its_tier_again_when_its_code_moves(tmp_path):
    code      = FakeCode()
    store     = KnowledgeStore(tmp_path, code=code)
    lesson, _ = store.note('When navigate_to stops short, send the goal again.', seen(by='agent:task'), {'skill': 'navigate_to'})
    place, _  = store.note('The cabin door closes after 8 s.', seen(), {'place': 'cabin'})

    store.confirm(lesson.id, seen(by='outcome:navigate_to', run='r2'))      # a step that followed it: evidence, not a source
    assert store.get(lesson.id).tier == 'observation' and store.get(lesson.id).reliability() > 0

    for claim in (lesson, place):
        store.confirm(claim.id, seen(by='check:arrived', run='r3'))

    assert store.get(lesson.id).tier == store.get(place.id).tier == 'belief'

    code.version = 2                                                        # the follower changed

    assert 'untried on this code' in store.prompt_section(observations=True)
    assert store.get(lesson.id).tier == 'observation' and store.get(place.id).tier == 'belief'   # no skill, no clock

    store.confirm(lesson.id, seen(by='check:arrived', run='r4'))            # one run on the new code is not enough
    store.confirm(lesson.id, seen(by='operator', run='r4'))
    assert store.get(lesson.id).tier == 'observation'

    store.confirm(lesson.id, seen(by='operator', run='r5'))
    store.contradict(lesson.id, seen(by='outcome:navigate_to', run='r6'))   # a failed step moves no tier
    assert store.get(lesson.id).tier == 'belief'

    store.contradict(lesson.id, seen(by='check:arrived', run='r7'))         # a contradiction moves a belief down
    assert store.get(lesson.id).tier == 'observation'


def test_a_session_gets_the_rules_and_an_index_and_a_model_with_no_tools_gets_the_claims(tmp_path):
    store = KnowledgeStore(tmp_path)
    store.note('When navigate_to stops short, send the goal again.', seen(by='agent:task'), {'skill': 'navigate_to'})

    session, model = store.guide(), store.guide(claims=True)

    assert 'skill navigate_to: 1 observation' in session and 'send the goal again' not in session
    assert 'send the goal again' in model and 'knowledge list' not in model


def test_a_skill_claim_with_no_record_of_its_code_counts_nothing_until_it_is_seen_again(tmp_path):
    claim, _ = KnowledgeStore(tmp_path).note('It stops short.', seen(), {'skill': 'navigate_to'})   # no clock here
    store    = KnowledgeStore(tmp_path, code=FakeCode())

    assert 'untried on this code' in store.prompt_section(observations=True)

    store.confirm(claim.id, seen(by='operator', run='r2'))

    assert store.get(claim.id).under == {'version': 1} and len(store.get(claim.id).counted.confirmations) == 1
