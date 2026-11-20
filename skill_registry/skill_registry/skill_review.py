"""A person's review of the skill library: what is in it, and keep or remove a skill that the agent wrote.

Mission Control's Skills tab uses this. It works on the files only. The executor's registry sees a change at its next
refresh: a kept skill loads the same (write_skill saves it at maturity untested), and a removed one unloads
(Registry.refresh). Git is the person's own step: Keep and Remove change files, and the person commits them.

- Keep takes the agent's mark lines away (AGENT_MARK, CODE_MARK). Then the skill is a person's: write_skill may no
  longer replace it, and a contract without the mark loads at the maturity it gives. The stamps follow the new
  bytes, because only a comment changed: the skill and the skills that use it do not go stale.
- Remove moves the skill's folder out of the library, into removed_dir with the time in its name, and drops its
  stamp. It never deletes. It refuses a skill that another skill uses (pre, effects, check, fallback, a call in code).
"""

import os
import time
import shutil
import hashlib

from pathlib import Path
from dataclasses import asdict

import yaml

from .contract import AGENT_MARK, ContractError, load_contract
from .provenance import file_digest
from .registry import CODE_FILE, code_file, contract_file, contract_files
from .skill_stamps import called_skills
from .skill_writer import CODE_MARK


class ReviewError(ValueError):
    pass


def _runs(stamps_path: Path | None) -> dict[str, int]:
    """The successful runs that skill_stamps kept for each skill; none when there is no stamps file"""
    try:
        stamps = yaml.safe_load(Path(stamps_path).read_text()) or {} if stamps_path else {}
    except (OSError, yaml.YAMLError):
        return {}

    return {name: len(stamp.get('runs') or []) for name, stamp in stamps.items() if isinstance(stamp, dict)}


def library(directory: Path, stamps_path: Path | None = None) -> list[dict]:
    """Each skill folder: its contract as fields (or the error that stops it loading), the file texts, whether the
    agent wrote it and is still waiting for a person, its runs, the skills that use it, and its version: Keep and
    Remove take the version a person saw, and refuse when the files changed since"""
    runs, skills = _runs(stamps_path), []

    for path in contract_files(directory):
        item = {'name': path.parent.name, 'agent': False, 'text': '', 'code': '', 'runs': runs.get(path.parent.name, 0),
                'contract': {}, 'error': '', 'version': ''}

        try:                                         # one file that cannot be read must not hide the others
            item['text'] = path.read_text()
            item['agent'] = item['text'].startswith(AGENT_MARK)
            code = path.parent / CODE_FILE
            item['code'] = code.read_text() if code.is_file() else ''
            item['version'] = _version(item['text'], item['code'])
            item['contract'] = asdict(load_contract(path))
        except (ContractError, OSError, ValueError, TypeError, yaml.YAMLError) as error:   # UnicodeDecodeError too
            item['error'] = str(error)

        skills.append(item)

    for item in skills:
        item['named_by'] = sorted(other['name'] for other in skills if item['name'] in _uses(other))

    return skills


def _uses(item: dict) -> list[str]:
    """The skills one skill names or calls: pre, effects, check, fallback, and the skills.run calls in its code"""
    contract = item['contract']

    return [*contract.get('pre', []), *contract.get('effects', []), *([contract['check']] if contract.get('check') else []),
            *contract.get('fallback', []), *(called_skills(item['code'])[0] if item['code'] else [])]


def _folder(directory: Path, name: str) -> Path:
    """The folder of a skill in the library, by its listed name only: a name from a page is never a path"""
    folders = {path.parent.name: path.parent for path in contract_files(directory)}

    if name not in folders:
        raise ReviewError(f'no skill {name!r} in the library')

    return folders[name]


def _write(path: Path, text: str):
    """A temp file, then one rename: the registry never reads half a file"""
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temporary.write_text(text)
    temporary.replace(path)


def _version(text: str, code: str) -> str:
    return hashlib.sha256(f'{text}\0{code}'.encode()).hexdigest()[:16]


def grade_version(directory: Path, name: str) -> str | None:
    """The version of a skill that a grade of its step belongs to: its contract and code, without the agent's marks.
    Keep takes only the marks away, so a kept skill keeps its grades; any other change is a new version. It is not
    the version the Skills tab shows (library()), which has the marks: that one says which files a person read"""
    contract = contract_file(name, directory)

    if not contract.is_file():
        return None

    return _version(contract.read_text().removeprefix(AGENT_MARK), _read(code_file(name, directory)).removeprefix(CODE_MARK))


def _read(path: Path) -> str:
    return path.read_text() if path.is_file() else ''


def _seen(folder: Path, name: str, version: str | None):
    """The files are still the version that the person read; None: no page, no check (a script, a test)"""
    if version is not None and _version((folder / 'contract.yaml').read_text(), _read(folder / CODE_FILE)) != version:
        raise ReviewError(f'{name} changed since you read it (the agent saved a new version): read it again')


def _stamps(stamps_path: Path | None, change):
    """Change the stamps file, a temp file then one rename as Stamps does. Nothing when there is no file"""
    if not stamps_path or not Path(stamps_path).is_file():
        return

    try:
        stamps = yaml.safe_load(Path(stamps_path).read_text()) or {}
    except yaml.YAMLError:
        return

    change(stamps)
    _write(Path(stamps_path), yaml.safe_dump(stamps, sort_keys=True))


def keep(directory: Path, name: str, stamps_path: Path | None = None, version: str | None = None) -> str:
    """Take the agent's marks away: the person has read the skill and keeps it"""
    folder        = _folder(directory, name)
    contract      = folder / 'contract.yaml'
    code          = folder / CODE_FILE
    text, program = contract.read_text(), _read(code)

    if not text.startswith(AGENT_MARK):
        raise ReviewError(f'{name} is not waiting for review: a person wrote it, or kept it already')

    _seen(folder, name, version)

    if program.startswith(CODE_MARK):
        _write(code, program.removeprefix(CODE_MARK))

    _write(contract, text.removeprefix(AGENT_MARK))
    digest = file_digest(contract)

    def follow(stamps: dict):
        """Its own digest and the digest that each skill using it keeps: only a comment changed"""
        for skill, stamp in stamps.items():
            if not isinstance(stamp, dict):
                continue

            if skill == name and 'self' in stamp:
                stamp['self'] = digest

            if name in (stamp.get('uses') or {}):
                stamp['uses'][name] = digest

    _stamps(stamps_path, follow)

    return f'kept {name}: commit it in git'


def remove(directory: Path, name: str, removed_dir: Path, stamps_path: Path | None = None, version: str | None = None) -> str:
    """Move an agent's skill out of the library. A person's skill is removed in git; a skill that another one names
    stays, because the other one would no longer load or run"""
    folder = _folder(directory, name)

    if not (folder / 'contract.yaml').read_text().startswith(AGENT_MARK):
        raise ReviewError(f'{name} was written or kept by a person: remove it in git')

    _seen(folder, name, version)

    if named_by := next((item['named_by'] for item in library(directory) if item['name'] == name), []):
        raise ReviewError(f'{", ".join(named_by)} use {name}: remove or change those first')

    target = Path(removed_dir) / f'{name}-{time.strftime("%Y%m%d-%H%M%S")}'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(target))
    _stamps(stamps_path, lambda stamps: stamps.pop(name, None))

    return f'removed {name}; it is in {target}'
