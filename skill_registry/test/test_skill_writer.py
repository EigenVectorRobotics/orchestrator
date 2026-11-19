import yaml

from skill_registry import Registry, parse_contract

from skill_registry.contract import AGENT_MARK

from skill_registry.skill_writer import SkillWriter
from skill_registry.skill_stamps import Stamps


SAY = {'name': 'say_it', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5, 'maturity': 'tested',
       'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'},
       'inputs': [{'name': 'text', 'type': 'string', 'field': 'data'}]}


def _file(directory, name: str):
    """A skill's contract file, in its own folder"""
    (directory / name).mkdir(parents=True, exist_ok=True)

    return directory / name / 'contract.yaml'


def _writer(tmp_path, type_errors=()):
    registry, loaded = Registry(), []
    writer           = SkillWriter(registry, tmp_path, lambda contract: list(type_errors), loaded.append)

    return writer, registry, loaded


def test_a_written_skill_is_saved_untested_and_loaded_at_once(tmp_path):
    writer, registry, loaded = _writer(tmp_path)

    reply = writer.write(yaml.safe_dump(SAY))

    assert reply['ok'] and registry.get('say_it').maturity == 'untested' and [c.name for c in loaded] == ['say_it']
    assert (tmp_path / 'say_it' / 'contract.yaml').read_text().startswith(AGENT_MARK)
    assert Registry.from_directory(tmp_path).get('say_it') == registry.get('say_it')     # it loads again as written


def test_a_version_that_the_registry_refuses_leaves_the_old_one_on_disk(tmp_path):
    saved = tmp_path / 'say_it' / 'contract.yaml'

    # the types change after the save: the writer's check passes, the registry's refuses
    def check(contract):
        on_disk = yaml.safe_load(saved.read_text())['timeout_s'] if saved.exists() else None
        return ['changed'] if contract.timeout_s == on_disk == 9 else []

    registry = Registry()
    writer   = SkillWriter(registry, tmp_path, check, lambda contract: None)

    assert writer.write(yaml.safe_dump(SAY))['ok']

    reply = writer.write(yaml.safe_dump(SAY | {'timeout_s': 9}))

    assert not reply['ok'] and registry.get('say_it').timeout_s == 5
    assert Registry.from_directory(tmp_path).get('say_it').timeout_s == 5          # a restart loads the old version
    assert not (tmp_path / 'say_it' / 'old').exists() or not any((tmp_path / 'say_it' / 'old').iterdir())



def test_a_new_skill_that_the_registry_refuses_leaves_no_folder(tmp_path):
    checks = []

    def check(contract):                    # passes before the save, refuses at the registry's refresh
        checks.append(contract.name)
        return ['refused'] if len(checks) > 1 else []

    writer = SkillWriter(Registry(), tmp_path, check, lambda contract: None)

    assert not writer.write(yaml.safe_dump(SAY))['ok'] and not any(tmp_path.iterdir())

def test_bad_denied_or_mistyped_contracts_are_not_saved(tmp_path):
    writer, registry, _ = _writer(tmp_path)
    _file(tmp_path, 'by_hand').write_text(yaml.safe_dump(SAY | {'name': 'by_hand'}))

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

    assert len(registry) == 0 and not (tmp_path / 'say_it' / 'contract.yaml').exists()

    writer, registry, _ = _writer(tmp_path, ['std_msgs/msg/String has no field datum'])
    assert 'no field datum' in writer.write(yaml.safe_dump(SAY))['message'] and len(registry) == 0


def test_the_registry_loads_new_and_changed_files_while_it_runs(tmp_path):
    _file(tmp_path, 'a').write_text(yaml.safe_dump(SAY | {'name': 'a'}))
    registry = Registry.from_directory(tmp_path)

    assert registry.refresh(tmp_path) == ([], {})

    _file(tmp_path, 'b').write_text(yaml.safe_dump(SAY | {'name': 'b'}))
    _file(tmp_path, 'c').write_text(yaml.safe_dump(SAY | {'name': 'c', 'interface': SAY['interface'] | {'name': '/lowcmd'}}))
    changed, errors = registry.refresh(tmp_path)

    assert changed == ['b'] and 'denied' in errors['c'] and 'c' not in registry
    assert registry.refresh(tmp_path) == ([], {})                       # a refused file is not read again until it changes
    assert parse_contract(SAY | {'name': 'b'}) == registry.get('b')


def test_a_file_written_on_disk_meets_the_same_rules_as_write_skill(tmp_path):
    _file(tmp_path, 'a').write_text(yaml.safe_dump(SAY | {'name': 'a'}))
    registry = Registry.from_directory(tmp_path)

    _file(tmp_path, 'mine').write_text(AGENT_MARK + yaml.safe_dump(SAY | {'name': 'mine'}))           # says tested
    _file(tmp_path, 'wrong_file').write_text(yaml.safe_dump(SAY | {'name': 'other'}))
    _file(tmp_path, 'needs').write_text(yaml.safe_dump(SAY | {'name': 'needs', 'fallback': ['nothing']}))
    _file(tmp_path, 'typed').write_text(yaml.safe_dump(SAY | {'name': 'typed'}))
    changed, errors = registry.refresh(tmp_path, lambda contract: ['no such field'] if contract.name == 'typed' else [])

    assert changed == ['mine'] and registry.get('mine').maturity == 'untested'
    assert 'must be other/' in errors['wrong_file'] and 'unknown fallback' in errors['needs']
    assert errors['typed'] == 'no such field'


def test_a_broken_file_is_refused_not_raised(tmp_path):
    _file(tmp_path, 'a').write_text(yaml.safe_dump(SAY | {'name': 'a'}))
    registry = Registry.from_directory(tmp_path)

    _file(tmp_path, 'half').write_text('name: half\nkind: [')
    _file(tmp_path, 'typo').write_text(yaml.safe_dump(SAY | {'name': 'typo', 'timeout_s': 'abc'}))
    changed, errors = registry.refresh(tmp_path)

    assert changed == [] and set(errors) == {'half', 'typo'}

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
    old = tmp_path / 'say_it' / 'old'
    assert (old / 'contract.1.yaml').exists()

    (old / 'contract.1.yaml').unlink()
    writer.write(yaml.safe_dump(SAY | {'timeout_s': 8}))

    assert sorted(file.name for file in old.iterdir()) == ['contract.2.yaml', 'contract.3.yaml']
    assert yaml.safe_load((tmp_path / 'say_it' / 'contract.yaml').read_text())['maturity'] == 'untested'


CODE_SKILL = {'name': 'greet', 'kind': 'action', 'device': 'nuc', 'timeout_s': 20,
              'interface': {'transport': 'code'}}
BODY       = 'def run(skills, inputs):\n    skills.run("say_it", text="hi")\n    return {}\n'


def test_a_code_skill_saves_its_python_beside_its_contract(tmp_path):
    writer, registry, _ = _writer(tmp_path / 'skills')

    reply = writer.write(yaml.safe_dump(CODE_SKILL), BODY)

    assert reply['ok'] and registry.get('greet').interface.transport == 'code'
    assert 'def run(skills, inputs)' in (tmp_path / 'skills' / 'greet' / 'code.py').read_text()


def test_a_code_skill_without_its_python_is_refused(tmp_path):
    writer, registry, _ = _writer(tmp_path / 'skills')

    assert not writer.write(yaml.safe_dump(CODE_SKILL))['ok']
    assert registry.get('greet') is None


def test_python_that_does_not_parse_or_has_no_run_is_refused(tmp_path):
    writer, registry, _ = _writer(tmp_path / 'skills')

    assert not writer.write(yaml.safe_dump(CODE_SKILL), 'def run(skills, inputs:\n')['ok']
    assert not writer.write(yaml.safe_dump(CODE_SKILL), 'def other(skills, inputs):\n    return {}\n')['ok']
    assert registry.get('greet') is None and not (tmp_path / 'skills' / 'greet').exists()


def test_a_new_version_keeps_the_old_python_and_a_refused_one_puts_it_back(tmp_path):
    writer, registry, _ = _writer(tmp_path / 'skills', type_errors=[])
    writer.write(yaml.safe_dump(CODE_SKILL), BODY)

    writer.write(yaml.safe_dump(CODE_SKILL), BODY.replace('hi', 'hello'))

    assert 'hello' in (tmp_path / 'skills' / 'greet' / 'code.py').read_text()
    assert 'hi' in (tmp_path / 'skills' / 'greet' / 'old' / 'code.1.py').read_text()

    writer.check_types = lambda contract: ['no such type']
    assert not writer.write(yaml.safe_dump(CODE_SKILL), BODY.replace('hi', 'third'))['ok']
    assert 'hello' in (tmp_path / 'skills' / 'greet' / 'code.py').read_text()        # the refused version left nothing behind


def test_python_sent_for_a_skill_that_is_not_code_is_refused(tmp_path):
    writer, registry, _ = _writer(tmp_path / 'skills')

    assert not writer.write(yaml.safe_dump(SAY), BODY)['ok']
    assert registry.get('say_it') is None


def test_a_written_skill_is_stamped_with_what_proved_it(tmp_path):
    """The seam: write_skill -> Stamps.write. Without it the stamp is never made and no skill is ever stale"""
    stamps           = Stamps(tmp_path, [], path=tmp_path / 'stamps.yaml')
    writer, _, _     = _writer(tmp_path)
    writer.stamps    = stamps

    assert writer.write(yaml.safe_dump(SAY))['ok']

    stamp = stamps.all()['say_it']

    assert stamp['self'] == stamps.digest('say_it')                  # the file it stamped is the file on disk
    assert stamps.reasons()['say_it']                                # no repo was found, so nothing proves it


def test_write_skill_writes_only_in_the_skills_own_folder_through_the_workspace_link(tmp_path):
    """The workspace reaches the library in git through a link; what write_skill saves lands there, and nowhere else"""
    library = tmp_path / 'repo' / 'skills'
    library.mkdir(parents=True)
    (tmp_path / 'agent').mkdir()
    (tmp_path / 'agent' / 'skills').symlink_to(library)
    writer, registry, _ = _writer(tmp_path / 'agent' / 'skills')

    assert writer.write(yaml.safe_dump(CODE_SKILL), BODY)['ok']
    assert writer.write(yaml.safe_dump(CODE_SKILL), BODY.replace('hi', 'hello'))['ok']

    written = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*') if path.is_file())
    assert written == ['repo/skills/greet/code.py', 'repo/skills/greet/contract.yaml',
                       'repo/skills/greet/old/code.1.py', 'repo/skills/greet/old/contract.1.yaml']
    assert Registry.from_directory(tmp_path / 'agent' / 'skills').get('greet') == registry.get('greet')
