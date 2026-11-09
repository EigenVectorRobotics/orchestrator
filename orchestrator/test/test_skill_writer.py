import yaml

from skill_registry import Registry, parse_contract

from skill_registry.contract import AGENT_MARK

from orchestrator.skill_writer import SkillWriter


SAY = {'name': 'say_it', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5, 'maturity': 'tested',
       'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'},
       'inputs': [{'name': 'text', 'type': 'string', 'field': 'data'}]}


def _writer(tmp_path, type_errors=()):
    registry, loaded = Registry(), []
    writer           = SkillWriter(registry, tmp_path, lambda contract: list(type_errors), loaded.append)

    return writer, registry, loaded


def test_a_written_skill_is_saved_untested_and_loaded_at_once(tmp_path):
    writer, registry, loaded = _writer(tmp_path)

    reply = writer.write(yaml.safe_dump(SAY))

    assert reply['ok'] and registry.get('say_it').maturity == 'untested' and [c.name for c in loaded] == ['say_it']
    assert (tmp_path / 'say_it.yaml').read_text().startswith(AGENT_MARK)
    assert Registry.from_directory(tmp_path).get('say_it') == registry.get('say_it')     # it loads again as written


def test_a_version_that_the_registry_refuses_leaves_the_old_one_on_disk(tmp_path):
    checks = []

    def check(contract):                            # passes the writer's check, fails the registry's (a change between)
        checks.append(contract.timeout_s)
        return ['changed'] if checks.count(9) == 2 else []

    registry = Registry()
    writer   = SkillWriter(registry, tmp_path, check, lambda contract: None)

    assert writer.write(yaml.safe_dump(SAY))['ok']

    reply = writer.write(yaml.safe_dump(SAY | {'timeout_s': 9}))

    assert not reply['ok'] and registry.get('say_it').timeout_s == 5
    assert Registry.from_directory(tmp_path).get('say_it').timeout_s == 5          # a restart loads the old version
    assert not (tmp_path / 'old').exists() or not any((tmp_path / 'old').iterdir())


def test_bad_denied_or_mistyped_contracts_are_not_saved(tmp_path):
    writer, registry, _ = _writer(tmp_path)
    (tmp_path / 'by_hand.yaml').write_text(yaml.safe_dump(SAY | {'name': 'by_hand'}))

    for text, why in [
        ('a: [', 'not YAML'),
        (yaml.safe_dump(SAY | {'name': 'Bad Name'}), 'snake_case'),
        (yaml.safe_dump(SAY | {'name': 'by_hand'}), 'written by a person'),
        (yaml.safe_dump(SAY | {'kind': 'fly'}), 'unknown kind'),
        (yaml.safe_dump(SAY | {'interface': SAY['interface'] | {'name': 'rt/lowcmd'}}), 'denied name'),
        (yaml.safe_dump(SAY | {'fallback': ['nothing']}), 'unknown fallback'),
    ]:
        reply = writer.write(text)
        assert not reply['ok'] and why in reply['message'], why

    assert len(registry) == 0 and not (tmp_path / 'say_it.yaml').exists()

    writer, registry, _ = _writer(tmp_path, ['std_msgs/msg/String has no field datum'])
    assert 'no field datum' in writer.write(yaml.safe_dump(SAY))['message'] and len(registry) == 0


def test_the_registry_loads_new_and_changed_files_while_it_runs(tmp_path):
    (tmp_path / 'a.yaml').write_text(yaml.safe_dump(SAY | {'name': 'a'}))
    registry = Registry.from_directory(tmp_path)

    assert registry.refresh(tmp_path) == ([], {})

    (tmp_path / 'b.yaml').write_text(yaml.safe_dump(SAY | {'name': 'b'}))
    (tmp_path / 'c.yaml').write_text(yaml.safe_dump(SAY | {'name': 'c', 'interface': SAY['interface'] | {'name': '/lowcmd'}}))
    changed, errors = registry.refresh(tmp_path)

    assert changed == ['b'] and 'denied' in errors['c.yaml'] and 'c' not in registry
    assert registry.refresh(tmp_path) == ([], {})                       # a refused file is not read again until it changes
    assert parse_contract(SAY | {'name': 'b'}) == registry.get('b')


def test_a_file_written_on_disk_meets_the_same_rules_as_write_skill(tmp_path):
    (tmp_path / 'a.yaml').write_text(yaml.safe_dump(SAY | {'name': 'a'}))
    registry = Registry.from_directory(tmp_path)

    (tmp_path / 'mine.yaml').write_text(AGENT_MARK + yaml.safe_dump(SAY | {'name': 'mine'}))           # says tested
    (tmp_path / 'wrong_file.yaml').write_text(yaml.safe_dump(SAY | {'name': 'other'}))
    (tmp_path / 'needs.yaml').write_text(yaml.safe_dump(SAY | {'name': 'needs', 'fallback': ['nothing']}))
    (tmp_path / 'typed.yaml').write_text(yaml.safe_dump(SAY | {'name': 'typed'}))
    changed, errors = registry.refresh(tmp_path, lambda contract: ['no such field'] if contract.name == 'typed' else [])

    assert changed == ['mine'] and registry.get('mine').maturity == 'untested'
    assert 'must be other.yaml' in errors['wrong_file.yaml'] and 'unknown fallback' in errors['needs.yaml']
    assert errors['typed.yaml'] == 'no such field'


def test_a_broken_file_is_refused_not_raised(tmp_path):
    (tmp_path / 'a.yaml').write_text(yaml.safe_dump(SAY | {'name': 'a'}))
    registry = Registry.from_directory(tmp_path)

    (tmp_path / 'half.yaml').write_text('name: half\nkind: [')
    (tmp_path / 'typo.yaml').write_text(yaml.safe_dump(SAY | {'name': 'typo', 'timeout_s': 'abc'}))
    changed, errors = registry.refresh(tmp_path)

    assert changed == [] and set(errors) == {'half.yaml', 'typo.yaml'}

    writer, _, _ = _writer(tmp_path)
    writer.check_types = lambda contract: [][1]                          # a check that crashes refuses, never raises
    assert 'IndexError' in writer.write(yaml.safe_dump(SAY | {'name': 'other'}))['message']


def test_a_new_version_must_still_fit_the_skills_that_name_it(tmp_path):
    writer, registry, _ = _writer(tmp_path)
    ready = {'name': 'ready', 'kind': 'condition', 'device': 'nuc',
             'interface': {'transport': 'watch', 'success_when': {'topic': '/ready', 'ros_type': 'std_msgs/msg/Bool'}}}

    assert writer.write(yaml.safe_dump(ready))['ok']
    assert writer.write(yaml.safe_dump(SAY | {'pre': ['ready']}))['ok']

    reply = writer.write(yaml.safe_dump(SAY | {'name': 'ready'}))                     # now an action

    assert not reply['ok'] and 'not a condition' in reply['message'] and registry.get('ready').kind == 'condition'
    assert Registry.from_directory(tmp_path)                                           # a restart still loads


def test_the_saved_file_is_untested_and_old_versions_are_never_overwritten(tmp_path):
    writer, registry, _ = _writer(tmp_path)

    for timeout in (5, 6, 7):
        reply = writer.write(yaml.safe_dump(SAY | {'timeout_s': timeout}))

    assert reply['ok'] and 'replaced' in reply['message'] and registry.get('say_it').timeout_s == 7
    assert (tmp_path / 'old' / 'say_it.1.yaml').exists()

    (tmp_path / 'old' / 'say_it.1.yaml').unlink()
    writer.write(yaml.safe_dump(SAY | {'timeout_s': 8}))

    assert sorted(file.name for file in (tmp_path / 'old').iterdir()) == ['say_it.2.yaml', 'say_it.3.yaml']
    assert yaml.safe_load((tmp_path / 'say_it.yaml').read_text())['maturity'] == 'untested'
