import shutil

import pytest
import yaml

from skill_registry import Registry
from skill_registry.contract import AGENT_MARK
from skill_registry.provenance import file_digest
from skill_registry.skill_review import ReviewError, composition, grade_version, keep, library, remove
from skill_registry.skill_writer import CODE_MARK, SkillWriter


SAY = {'name': 'say_it', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5,
       'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'},
       'inputs': [{'name': 'text', 'type': 'string', 'field': 'data'}]}


def _write(directory, name: str, agent: bool = True, **fields):
    (directory / name).mkdir(parents=True, exist_ok=True)
    (directory / name / 'contract.yaml').write_text((AGENT_MARK if agent else '') + yaml.safe_dump(SAY | {'name': name} | fields))


def _gone(directory, *names):
    for name in names:
        shutil.rmtree(directory / name)


def test_a_skill_whose_folder_is_gone_twice_unloads_unless_a_staying_skill_uses_it(tmp_path):
    _write(tmp_path, 'a')
    _write(tmp_path, 'b', fallback=['a'])
    _write(tmp_path, 'c')
    _write(tmp_path, 'd')
    _write(tmp_path, 'e')
    _write(tmp_path, 'f')
    _write(tmp_path, 'walk', interface={'transport': 'code'}, inputs=[])
    (tmp_path / 'walk' / 'code.py').write_text('def run(skills, inputs):\n    skills.run("e")\n    return {}\n')
    _write(tmp_path, 'g', fallback=['f'])
    registry = Registry.from_directory(tmp_path)

    _gone(tmp_path, 'a', 'c', 'e', 'f', 'g')

    assert registry.refresh(tmp_path) == ([], {}) and 'c' in registry       # once missing may be a gap: no unload

    changed, errors = registry.refresh(tmp_path)

    assert changed == [] and 'c' not in registry
    assert 'a' in registry and 'b use it' in errors['a']                     # b stays and names it
    assert 'e' in registry and 'walk use it' in errors['e']                  # walk's code calls it
    assert 'f' not in registry and 'g' not in registry                       # its only user went too
    assert registry.refresh(tmp_path) == ([], {})                           # said once, not at every refresh


def test_an_empty_or_missing_library_unloads_nothing(tmp_path):
    library_dir = tmp_path / 'skills'
    _write(library_dir, 'a')
    registry = Registry.from_directory(library_dir)
    _gone(library_dir, 'a')

    for _ in range(3):                                                    # a git checkout, a link made again
        registry.refresh(library_dir)
        registry.refresh(tmp_path / 'nowhere')

    assert 'a' in registry


def test_the_library_lists_each_skill_with_who_wrote_it_its_runs_and_who_names_it(tmp_path):
    _write(tmp_path, 'a')
    _write(tmp_path, 'b', agent=False, fallback=['a'])
    (tmp_path / 'broken').mkdir()
    (tmp_path / 'broken' / 'contract.yaml').write_text('name: broken\nkind: [')
    stamps = tmp_path / 'stamps.yaml'
    stamps.write_text(yaml.safe_dump({'a': {'runs': ['r1', 'r2']}}))

    _write(tmp_path, 'binary')
    (tmp_path / 'binary' / 'code.py').write_bytes(b'\xff\xfe')                 # not UTF-8: one item's error, not the list's

    items = {item['name']: item for item in library(tmp_path, stamps)}

    assert items['binary']['error'] and items['a']['version'] and not items['a']['error']

    assert items['a']['agent'] and not items['b']['agent'] and items['a']['runs'] == 2 and items['b']['runs'] == 0
    assert items['a']['named_by'] == ['b'] and items['a']['contract']['interface']['name'] == '/speak'
    assert items['broken']['error'] and items['broken']['contract'] == {}


def test_keep_takes_the_marks_away_and_the_skill_stays_untested_with_its_version_for_grades(tmp_path):
    writer = SkillWriter(Registry(), tmp_path, lambda contract: [], lambda contract: None)
    code   = 'def run(skills, inputs):\n    return {}\n'
    assert writer.write(yaml.safe_dump(SAY | {'name': 'walk', 'maturity': 'tested', 'interface': {'transport': 'code'},
                                              'inputs': []}), code)['ok']

    stamps = tmp_path / 'stamps.yaml'
    stamps.write_text(yaml.safe_dump({'walk': {'self': 'old', 'runs': ['r1']}, 'other': {'uses': {'walk': 'old'}}}))
    version = next(item['version'] for item in library(tmp_path) if item['name'] == 'walk')
    graded  = grade_version(tmp_path, 'walk')

    with pytest.raises(ReviewError, match='changed since you read it'):
        keep(tmp_path, 'walk', stamps, version='not what is on disk')

    assert 'commit it' in keep(tmp_path, 'walk', stamps, version)
    digest = file_digest(tmp_path / 'walk' / 'contract.yaml')                # only a comment changed: still proved
    assert yaml.safe_load(stamps.read_text()) == {'walk': {'self': digest, 'runs': ['r1']}, 'other': {'uses': {'walk': digest}}}

    assert not (tmp_path / 'walk' / 'contract.yaml').read_text().startswith(AGENT_MARK)
    assert not (tmp_path / 'walk' / 'code.py').read_text().startswith(CODE_MARK)
    assert Registry.from_directory(tmp_path).get('walk').maturity == 'untested'    # the agent's "tested" is not kept
    assert grade_version(tmp_path, 'walk') == graded                         # only the marks went: its grades stay
    (tmp_path / 'walk' / 'code.py').write_text(code.replace('{}', '{"x": 1}'))
    assert grade_version(tmp_path, 'walk') != graded and grade_version(tmp_path, 'nothing') is None

    with pytest.raises(ReviewError, match='not waiting for review'):
        keep(tmp_path, 'walk')

    assert 'written by a person' in writer.write(yaml.safe_dump(SAY | {'name': 'walk', 'interface': {'transport': 'code'},
                                                                        'inputs': []}), code)['message']


def test_remove_moves_only_an_agent_skill_that_nothing_names(tmp_path):
    library_dir, removed = tmp_path / 'skills', tmp_path / 'removed'
    _write(library_dir, 'a')
    _write(library_dir, 'b', fallback=['a'])
    _write(library_dir, 'mine', agent=False)
    _write(library_dir, 'said')
    _write(library_dir, 'walk', interface={'transport': 'code'}, inputs=[])
    (library_dir / 'walk' / 'code.py').write_text('def run(skills, inputs):\n    skills.run("said")\n    return {}\n')

    with pytest.raises(ReviewError, match='b use a'):
        remove(library_dir, 'a', removed)

    with pytest.raises(ReviewError, match='walk use said'):
        remove(library_dir, 'said', removed)

    with pytest.raises(ReviewError, match='by a person'):
        remove(library_dir, 'mine', removed)

    with pytest.raises(ReviewError, match='no skill'):
        remove(library_dir, '../skills/b', removed)                       # a name from a page is never a path

    stamps = tmp_path / 'stamps.yaml'
    stamps.write_text(yaml.safe_dump({'b': {'self': 'x'}, 'a': {'self': 'y'}}))

    assert 'removed b' in remove(library_dir, 'b', removed, stamps)
    assert yaml.safe_load(stamps.read_text()) == {'a': {'self': 'y'}}       # no stale stamp of a skill that is gone
    assert not (library_dir / 'b').exists() and [path.name.split('-')[0] for path in removed.iterdir()] == ['b']
    assert (next(removed.iterdir()) / 'contract.yaml').is_file()          # moved, not deleted


def test_a_composition_names_every_skill_it_calls_and_the_version_of_each_code_skill(tmp_path):
    for name, code in (('ride', 'def run(skills, inputs):\n    skills.run("walk")\n    skills.run("say", text="up")\n'),
                       ('walk', 'def run(skills, inputs):\n    skills.run("navigate_to")\n    skills.run(inputs["any"])\n'),
                       ('navigate_to', ''), ('say', '')):
        _write(tmp_path, name, interface={'transport': 'code'} if code else SAY['interface'], inputs=[])
        if code:
            (tmp_path / name / 'code.py').write_text(code)

    found = composition(tmp_path, 'ride')

    assert found['calls'] == ['walk', 'say', 'navigate_to'] and found['unknown'] is True
    assert found['versions'] == {'ride': grade_version(tmp_path, 'ride'), 'walk': grade_version(tmp_path, 'walk')}
    assert composition(tmp_path, 'say') == {'calls': [], 'versions': {}, 'unknown': False}
