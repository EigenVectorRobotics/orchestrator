"""write_skill: the agent writes a contract (docs/skill-discovery.md). It is checked (the schema, the deny list, the
references, the ROS types), saved in its own folder of the library with AGENT_MARK, and loaded through Registry.refresh,
the same way in as a file that changes on disk, so the next step can run it.

A code skill (transport code) is two files in its folder: the contract, and the Python that calls other skills. Both are saved
together and both go back if either is refused, so the library never holds a contract without its code. A tree skill
(transport tree) is the same, with tree.xml: a BehaviorTree of skills the library knows, checked by the tree validator.
Registry.cloud_part says when a composition needs the cloud: it reads the parts when asked.

The agent does not grade its own skills: a file with the mark loads at maturity untested. It may write a new version
of a skill it wrote (the old files go to old/ in the skill's folder), never one that a person wrote. This module needs no ROS: the
executor gives it the ROS type check and what to do with a loaded contract.
"""

import ast
import threading

from typing import Callable
from pathlib import Path

import yaml

from .contract import AGENT_MARK, Contract, ContractError, parse_contract
from .registry import CODE_FILE, TREE_FILE, Registry, body_file, code_file, contract_file, tree_file
from .validator import TreeValidator, ValidatorConfig, tree_skills

CODE_MARK = '# written by the agent with write_skill; it runs in the sandbox and calls skills through the executor\n'
TREE_MARK = '<!-- written by the agent with write_skill; it runs as one step, and each leaf is a skill -->\n'
BODY_MARKS = {CODE_FILE: CODE_MARK, TREE_FILE: TREE_MARK}     # the agent's mark at the top of each body file


class SkillWriter:
    def __init__(self, registry: Registry, directory: Path, check_types: Callable[[Contract], list[str]],
                 loaded: Callable[[Contract], None], stamps=None):
        self.registry    = registry
        self.directory   = Path(directory)               # the library: one folder per skill
        self.check_types = check_types            # contract -> what does not match its ROS types
        self.loaded      = loaded                 # e.g. the runner makes its clients for the new skill
        self.stamps      = stamps                 # skill_stamps.Stamps: what this skill was proved against; None: off
        self._lock       = threading.Lock()       # the timer's refresh must not read a file between its save and its check

    def write(self, text: str, code: str = '', tree: str = '') -> dict:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            return _refused(f'not YAML: {error}')

        if not isinstance(raw, dict):
            return _refused('a contract is a YAML mapping')

        raw = raw | {'maturity': 'untested'}           # saved so too: the mark may be removed by a person

        try:
            contract = parse_contract(raw)
        except (ContractError, TypeError, ValueError) as error:
            return _refused(str(error))

        path     = contract_file(contract.name, self.directory)
        replaced = path.exists()

        if replaced and not path.read_text().startswith(AGENT_MARK):
            return _refused(f'{contract.name} was written by a person; choose another name')

        code_path = code_file(contract.name, self.directory)
        tree_path = tree_file(contract.name, self.directory)

        if problem := _code_problem(contract, code, code_path) or self._tree_problem(contract, tree, tree_path):
            return _refused(problem)

        body_path = body_file(contract.interface.transport, contract.name, self.directory)
        body      = code or tree

        errors = self.registry.admit_errors(contract) or self.type_errors(contract)

        if errors:
            return _refused('; '.join(errors))

        with self._lock:
            saved = [(body_path, self._save_body(body_path, body))] if body else []    # the body first, so a contract
            saved += [(other, _move_aside(other)) for other in self._other_bodies(contract)]   # a new transport's old body
            saved.append((path, self._save(path, raw)))                                # never loads alone

            changed, errors = self.registry.refresh(self.directory, self.type_errors)

            if contract.name in errors:             # the registry keeps the old version: so does the folder
                for written, old in saved:
                    _put_back(written, old)

        for name in changed:
            self.loaded(self.registry.get(name))

        if contract.name in errors:
            return _refused(errors[contract.name])

        if self.stamps is not None:               # after the save: the stamp reads the files that are now on disk
            self.stamps.write(contract)           # write_skill is the only thing that proves a skill

        message = f'{contract.name} {"replaced" if replaced else "added"}, maturity untested'

        return {'ok': True, 'message': message + (', with its code' if code else ', with its tree' if tree else ''),
                'path': str(path.parent)}

    def type_errors(self, contract: Contract) -> list[str]:
        try:
            return self.check_types(contract)
        except ModuleNotFoundError as error:
            return [f'{contract.interface.ros_type}: the package {error.name} is not built on this machine']
        except Exception as error:                  # a check, not a crash: any failure here refuses the contract
            return [f'{contract.interface.ros_type}: {error!r}']

    def refresh(self) -> list[str]:
        """From the executor's timer: files that changed on disk (a person's edit) load the same way"""
        with self._lock:
            changed, _ = self.registry.refresh(self.directory, self.type_errors)

        for name in changed:
            self.loaded(self.registry.get(name))

        return changed

    def _save_body(self, path: Path, body: str) -> Path | None:
        """The Python of a code skill or the XML of a tree skill, beside its contract, with the agent's mark; an
        earlier version goes to old/"""
        path.parent.mkdir(parents=True, exist_ok=True)
        moved = _move_aside(path)
        mark  = BODY_MARKS[path.name]
        path.write_text(mark + body.removeprefix(mark))

        return moved

    def _other_bodies(self, contract: Contract) -> list[Path]:
        """The body files of the other transports: after code became a tree (or back), the old one must not stay, or
        the gate, the version and the parts would still read it"""
        own = body_file(contract.interface.transport, contract.name, self.directory)
        return [path for name in BODY_MARKS if (path := contract_file(contract.name, self.directory).parent / name) != own
                and path.is_file()]

    def _tree_problem(self, contract: Contract, tree: str, tree_path: Path) -> str:
        """Why the tree of a tree skill is not usable; '' if it is, or if the skill needs none"""
        if contract.interface.transport != 'tree':
            return 'a tree is only for a skill with transport tree' if tree else ''

        if not tree:
            return '' if tree_path.is_file() else f'{contract.name} is a tree skill, so it needs its tree too (--tree)'

        if contract.name in tree_skills(tree):
            return f'the tree of {contract.name} runs {contract.name}: a tree cannot run itself'

        config = ValidatorConfig(require_fallback=False)   # as for a step: the agent sees the tree's result and recovers
        errors = [issue for issue in TreeValidator(self.registry, config).validate(tree) if issue.level == 'error']

        return '; '.join(f'{issue.path}: {issue.message}' for issue in errors)

    def _save(self, path: Path, raw: dict) -> Path | None:
        """Write the file; an earlier version goes to old/. Returns where it went"""
        path.parent.mkdir(parents=True, exist_ok=True)
        moved = _move_aside(path)

        path.write_text(AGENT_MARK + yaml.safe_dump(raw, sort_keys=False))

        return moved


def _move_aside(path: Path) -> Path | None:
    """An earlier version goes to old/ beside it, numbered; returns where it went"""
    if not path.exists():
        return None

    old    = path.parent / 'old'
    suffix = path.suffix
    old.mkdir(exist_ok=True)
    numbers = [int(file.suffixes[-2][1:]) for file in old.glob(f'{path.stem}.*{suffix}') if file.suffixes[-2][1:].isdigit()]

    return path.rename(old / f'{path.stem}.{max(numbers, default=0) + 1}{suffix}')


def _put_back(path: Path, old: Path | None):
    """The contract did not load: the folder goes back to what it held"""
    path.unlink(missing_ok=True)

    if old:
        old.rename(path)
    elif not any(path.parent.iterdir()):        # a new skill's folder: the library keeps no empty folder
        path.parent.rmdir()


def _code_problem(contract: Contract, code: str, code_path: Path) -> str:
    """Why the code of a code skill is not usable; '' if it is, or if the skill needs none"""
    if contract.interface.transport != 'code':
        return 'code is only for a skill with transport code' if code else ''


    if not code:
        return '' if code_path.is_file() else f'{contract.name} is a code skill, so it needs its Python too'

    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return f'the code does not parse: line {error.lineno}: {error.msg}'

    if not any(isinstance(node, ast.FunctionDef) and node.name == 'run' for node in tree.body):
        return 'the code needs def run(skills, inputs) at the top level'

    return ''


def _refused(message: str) -> dict:
    return {'ok': False, 'message': f'not saved: {message}'}
