"""A skill is stamped with what proved it: its repos, and the version of every skill it runs."""

import subprocess

from pathlib import Path

import yaml

from skill_registry import Registry, parse_contract

from orchestrator.skill_stamps import Stamps, uses_of


SAY = {'name': 'say', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5.0,
       'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'},
       'inputs': [{'name': 'text', 'type': 'string', 'field': 'data'}]}

REPORT = {'name': 'report', 'kind': 'action', 'device': 'nuc', 'timeout_s': 20.0, 'source': 'navigation_stack',
          'interface': {'transport': 'code'}}

CODE = 'def run(skills, inputs):\n    skills.run("say", text="here")\n    return {}\n'


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / 'file.txt').write_text('one\n')

    for command in (['init', '-q'], ['add', '.'], ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'one']):
        subprocess.run(['git', '-C', str(path), *command], check=True, capture_output=True)

    return path


def _stamps(tmp_path: Path) -> Stamps:
    contracts = tmp_path / 'agent' / 'contracts'
    code      = tmp_path / 'agent' / 'code'
    contracts.mkdir(parents=True)
    code.mkdir(parents=True)

    for raw in (SAY, REPORT):
        (contracts / f'{raw["name"]}.yaml').write_text(yaml.safe_dump(raw))

    (code / 'report.py').write_text(CODE)                            # a code skill: the stamp reads it from disk
    repo = _repo(tmp_path / 'src' / 'navigation_stack')
    (repo / 'untracked').mkdir()                                     # git lists an untracked directory as one entry

    return Stamps(contracts, [str(repo.parent)], code_dir=code, sweep_s=0.0)   # sweep git on every call


def test_a_code_skill_declares_the_skills_it_calls_but_not_its_fallbacks():
    contract = parse_contract(REPORT | {'check': 'arrived', 'fallback': ['walk_out']})

    assert uses_of(contract, CODE) == (['arrived', 'say'], False)
    assert uses_of(contract, 'def run(skills, inputs):\n    skills.run(inputs["what"])\n') == (['arrived'], True)


def test_a_skill_goes_stale_when_a_skill_it_uses_changes_or_goes(tmp_path):
    stamps = _stamps(tmp_path)

    stamps.write(parse_contract(SAY))
    stamps.write(parse_contract(REPORT))

    assert stamps.reasons() == {}

    (stamps.contracts / 'say.yaml').write_text(yaml.safe_dump(SAY | {'timeout_s': 9.0}))    # a new version of the callee

    reasons = stamps.reasons()
    assert 'say' in reasons['report'] and 'say' in reasons           # the caller by the link, the callee by its own file

    stamps.ran('report', '0001-task')                                # a step that succeeded is evidence, not proof
    assert stamps.all()['report']['runs'] == ['0001-task'] and 'say' in stamps.reasons()['report']

    stamps.write(parse_contract(SAY))                                # the callee is proved again, its caller is not
    assert 'say' in stamps.reasons()['report'] and 'say' not in stamps.reasons()

    stamps.write(parse_contract(REPORT))                             # only the caller, written against the new callee
    assert stamps.reasons() == {} and 'runs' not in stamps.all()['report']    # a new stamp: the old evidence goes

    (stamps.contracts / 'say.yaml').unlink()

    assert 'say' in stamps.reasons()['report']
    assert 'say' in stamps.reasons(Registry([parse_contract(REPORT)]))['report']


def test_a_skill_goes_stale_when_the_repo_it_declares_moves(tmp_path):
    stamps = _stamps(tmp_path)

    stamps.write(parse_contract(REPORT))

    assert stamps.reasons() == {}

    (tmp_path / 'src' / 'navigation_stack' / 'file.txt').write_text('two\n')                # an uncommitted edit counts

    assert 'navigation_stack' in stamps.reasons()['report']
