from pathlib import Path

import pytest

from skill_registry import IncludeError, Registry, TreeValidator, ValidatorConfig, load_tree


PACKAGE   = Path(__file__).resolve().parents[1]
CONTRACTS = PACKAGE / 'contracts'


def _tree(tree_id: str, body: str = '<AlwaysSuccess/>') -> str:
    return f'  <BehaviorTree ID="{tree_id}">\n    {body}\n  </BehaviorTree>\n'


def _file(directory: Path, name: str, *parts: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<root BTCPP_format="4">\n' + ''.join(parts) + '</root>\n')
    return path


def test_an_include_is_replaced_by_the_trees_of_its_file(tmp_path):
    _file(tmp_path, 'sub/steps.xml', _tree('step'))
    main = _file(tmp_path, 'main.xml', _tree('main', '<SubTree ID="step"/>'), '  <include path="sub/steps.xml"/>\n')

    text = load_tree(main)

    assert '<include' not in text
    assert text.index('ID="main"') < text.index('ID="step"')
    assert TreeValidator(Registry.from_directory(CONTRACTS), ValidatorConfig()).is_valid(text)


def test_includes_resolve_relative_to_the_including_file_and_a_shared_file_comes_once(tmp_path):
    _file(tmp_path, 'sub/common.xml', _tree('common'))
    _file(tmp_path, 'sub/a.xml', _tree('a'), '  <include path="common.xml"/>\n')
    _file(tmp_path, 'sub/b.xml', _tree('b'), '  <include path="common.xml"/>\n')
    main = _file(tmp_path, 'main.xml', _tree('main'), '  <include path="sub/a.xml"/>\n  <include path="sub/b.xml"/>\n')

    assert load_tree(main).count('ID="common"') == 1


def test_a_missing_file_a_cycle_and_a_duplicate_id_are_errors(tmp_path):
    with pytest.raises(IncludeError, match='not found'):
        load_tree(_file(tmp_path, 'missing.xml', _tree('main'), '  <include path="nowhere.xml"/>\n'))

    _file(tmp_path, 'loop_b.xml', _tree('b'), '  <include path="loop_a.xml"/>\n')
    with pytest.raises(IncludeError, match='cycle'):
        load_tree(_file(tmp_path, 'loop_a.xml', _tree('a'), '  <include path="loop_b.xml"/>\n'))

    _file(tmp_path, 'twice.xml', _tree('main'))
    with pytest.raises(IncludeError, match='more than once'):
        load_tree(_file(tmp_path, 'dup.xml', _tree('main'), '  <include path="twice.xml"/>\n'))


def test_the_validator_refuses_an_unresolved_include():
    text = '<root BTCPP_format="4">\n' + _tree('main') + '  <include path="x.xml"/>\n</root>\n'

    assert TreeValidator(Registry.from_directory(CONTRACTS), ValidatorConfig()).validate(text)[0].message.startswith('<include> is not resolved')


