import os
import logging
import threading

from pathlib import Path
from dataclasses import dataclass

import yaml

from .deny import DenyList
from .contract import Contract, ContractError, load_contract


CONTRACT_FILE = 'contract.yaml'          # each skill is a folder: <skills_dir>/<name>/contract.yaml
CODE_FILE     = 'code.py'                # and, for a code skill, <skills_dir>/<name>/code.py

# The agent folder's names; the launch file and scripts/link_agent_dir.sh use the same
SKILLS     = 'skills'
KNOWLEDGE  = 'knowledge'
REMOVED    = 'removed_skills'                # skill folders a person removed (skill_review.remove); not in git
STAMPS     = 'stamps.yaml'
RUNBOOK    = 'runbook.md'


def workspace() -> Path:
    """The colcon workspace this runs from: $EIGEN_VECTOR_WS (the deploy's env.sh sets it), else home"""
    return Path(os.environ.get('EIGEN_VECTOR_WS', Path.home()))


def agent_dir() -> Path:
    """The agent's folder in the workspace: links to what is in git (skills/, runbook.md, docs/, made by
    scripts/link_agent_dir.sh), and what changes on each run (knowledge/, stamps.yaml)"""
    return workspace() / 'agent'


def skills_dir() -> Path:
    """The skill library: a link to this package's skills/ folder, so what write_skill saves is a change in git"""
    return agent_dir() / SKILLS


def agent_knowledge_dir() -> Path:
    """The knowledge store (claims.yaml, log.jsonl), beside the library it is about"""
    return agent_dir() / KNOWLEDGE


def stamps_file() -> Path:
    """What each skill was proved against (skill_stamps.py). Not in git: each step adds a run, and it names this
    machine's repos"""
    return agent_dir() / STAMPS


def agent_runbook_file() -> Path:
    """What the robot is, for the agent's prompt. The operator's file, like deny.yaml: the agent never writes it"""
    return agent_dir() / RUNBOOK


def contract_file(name: str, directory) -> Path:
    return Path(directory) / name / CONTRACT_FILE


def code_file(name: str, directory) -> Path:
    """Where the Python of a code skill lives; the runner and write_skill must agree on it"""
    return Path(directory) / name / CODE_FILE


def contract_files(directory) -> list[Path]:
    """The contract of each skill folder, by name"""
    return sorted(Path(directory).glob(f'*/{CONTRACT_FILE}'))


def missing_folder(directory) -> str:
    """'' if the folder is there; else why no skill loads, e.g. a typo in skills_dir or a broken link"""
    if Path(directory).is_dir():
        return ''

    if Path(directory).is_symlink():
        return f'{directory} is a broken link (to {os.readlink(directory)}): no skill loads; run scripts/link_agent_dir.sh'

    return f'no folder {directory}: no skill loads until it is made'


class Registry:
    """All known skill contracts, and which of them are live now"""

    def __init__(self, contracts: list[Contract] | None = None, deny: DenyList | None = None):
        self._contracts: dict[str, Contract] = {}
        self.deny                            = deny if deny is not None else DenyList.load()
        self.denied:     dict[str, list[str]] = {}  # skill name -> why it was left out
        self._file_times: dict[str, float]    = {}  # refresh: skill folder -> the mtime of its contract when last read
        self._gone_once:  set[str]            = set()  # folders missing at the last refresh: gone if missing again
        self._refreshing                      = threading.Lock()

        for contract in contracts or []:
            self.add(contract)

    @classmethod
    def from_directory(cls, directory: Path, deny: DenyList | None = None) -> 'Registry':
        """The contracts in directory, without those the deny list refuses (see registry.denied); none if it is empty"""
        paths    = contract_files(directory)
        registry = cls([load_contract(path) for path in paths], deny)
        registry._drop_dependents_of_denied()
        registry.check_references()
        registry._file_times = {path.parent.name: path.stat().st_mtime for path in paths}

        return registry

    def add(self, contract: Contract) -> bool:
        """False if the deny list refuses the contract: it is kept out, with the reasons in denied"""
        if contract.name in self._contracts:
            raise ContractError(f'duplicate skill {contract.name}')

        reasons = self.deny.check(contract)

        if reasons:
            self.denied[contract.name] = reasons
            logging.getLogger('skill_registry').warning(f'{contract.name} not loaded, the deny list refuses it: '
                                                        + '; '.join(reasons))
            return False

        self._contracts[contract.name] = contract
        self.denied.pop(contract.name, None)
        return True

    def _drop_dependents_of_denied(self):
        """A skill that names a denied skill (pre, effects, check, fallback) is left out too, until none is left"""
        while True:
            needs = {contract.name: sorted(set(contract.references) & set(self.denied)) for contract in self}
            needs = {name: denied for name, denied in needs.items() if denied}

            if not needs:
                return

            for name, denied in needs.items():
                del self._contracts[name]
                self.denied[name] = [f'it needs the denied skill {skill}' for skill in denied]
                logging.getLogger('skill_registry').warning(f'{name} not loaded: {"; ".join(self.denied[name])}')

    def admit_errors(self, contract: Contract) -> list[str]:
        """Why a new or changed contract may not join: the deny list, its references, the skills that name it"""
        return self.deny.check(contract) or self.reference_errors(contract) or self._dependent_errors(contract)

    def refresh(self, directory: Path, check=lambda contract: []) -> tuple[list[str], dict[str, str]]:
        """The one way in while it runs: load the files that are new or changed (a contract the agent wrote, or a
        person's edit), each through admit_errors and check (e.g. its ROS types). Returns the skills that changed,
        and the skill folders that did not load with why. A skill whose folder is gone unloads (_unload_gone)"""
        with self._refreshing:
            pending, errors = {}, {}
            paths           = contract_files(directory)
            self._unload_gone(directory, {path.parent.name for path in paths}, errors)

            for path in paths:
                folder = path.parent.name
                mtime  = path.stat().st_mtime

                if self._file_times.get(folder) == mtime:
                    continue

                self._file_times[folder] = mtime

                try:
                    contract = load_contract(path)
                except (ContractError, OSError, ValueError, TypeError, yaml.YAMLError) as error:   # e.g. a half-saved edit
                    errors[folder] = str(error)
                    continue

                if self.get(contract.name) != contract:
                    pending[folder] = contract

            changed = self._admit_all(pending, check, errors)

        for folder, error in errors.items():
            logging.getLogger('skill_registry').warning(f'{folder} not loaded: {error}')

        return changed, errors

    def _unload_gone(self, directory: Path, folders: set[str], errors: dict[str, str]):
        """A skill folder that was read and is missing at two refreshes in a row (a person removed it) unloads. Not
        while the library is missing or empty: a link made again or a git checkout is a gap, not a removal. A skill
        that a staying skill uses (pre, effects, check, fallback, or a call in its code) stays loaded until a restart,
        because that one would break; it is said once"""
        if missing_folder(directory) or not folders:
            return

        gone            = {name for name in self._file_times if name not in folders}
        confirmed       = gone & self._gone_once
        self._gone_once = gone - confirmed
        leaving         = set(confirmed)

        while users := {name: sorted(other.name for other in self if other.name not in leaving
                                     and name in self._uses(other, directory)) for name in leaving}:
            staying = {name: names for name, names in users.items() if names}

            if not staying:
                break

            for name, names in staying.items():
                errors[name] = f'its folder is gone, but {", ".join(names)} use it: it stays loaded until a restart'

            leaving -= set(staying)

        for name in confirmed:
            del self._file_times[name]

        for name in leaving:
            if self._contracts.pop(name, None) is not None:
                logging.getLogger('skill_registry').info(f'{name} unloaded: its folder is gone')

    @staticmethod
    def _uses(contract: Contract, directory: Path) -> list[str]:
        """The skills this one names, and for a code skill the skills its Python calls"""
        from .skill_stamps import called_skills      # skill_stamps imports this module

        try:
            code = code_file(contract.name, directory).read_text() if contract.interface.transport == 'code' else ''
        except OSError:
            code = ''

        return [*contract.references, *called_skills(code)[0]] if code else list(contract.references)

    def _admit_all(self, pending: dict[str, Contract], check, errors: dict[str, str]) -> list[str]:
        """Admit in passes, so a contract may name another one of the same batch"""
        changed = []

        while pending:
            refused = {}

            for file_name, contract in pending.items():
                reasons = self.admit_errors(contract) or check(contract)

                if reasons:
                    refused[file_name] = reasons
                else:
                    self._contracts[contract.name] = contract
                    self.denied.pop(contract.name, None)
                    changed.append(contract.name)

            if len(refused) == len(pending):
                errors |= {file_name: '; '.join(reasons) for file_name, reasons in refused.items()}
                return changed

            pending = {file_name: pending[file_name] for file_name in refused}

        return changed

    def get(self, name: str) -> Contract | None:
        return self._contracts.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._contracts

    def __iter__(self):
        return iter(self._contracts.values())

    def __len__(self) -> int:
        return len(self._contracts)

    def check_references(self):
        """Every pre, effect, check and fallback names a known skill of the right kind"""
        for contract in self:
            errors = self.reference_errors(contract)

            if errors:
                raise ContractError(errors[0])

    def _dependent_errors(self, contract: Contract) -> list[str]:
        """A new version must still fit the skills that name it, e.g. a condition must stay a condition"""
        old, self._contracts[contract.name] = self._contracts.get(contract.name), contract

        try:
            return [error for other in self if contract.name in other.references and other is not contract
                    for error in self.reference_errors(other)]
        finally:
            if old is None:
                del self._contracts[contract.name]
            else:
                self._contracts[contract.name] = old

    def reference_errors(self, contract: Contract) -> list[str]:
        errors = []

        for condition in contract.conditions:
            target = self.get(condition)

            if target is None:
                errors.append(f'{contract.name}: unknown condition {condition}')
            elif target.kind != 'condition':
                errors.append(f'{contract.name}: {condition} is a {target.kind}, not a condition')

        return errors + [f'{contract.name}: unknown fallback {fallback}' for fallback in contract.fallback if fallback not in self]


@dataclass
class GraphSnapshot:
    """Names in the ROS graph at one moment: services with a server, topics with a publisher or a subscriber"""

    services:    set[str]
    published:   set[str]
    subscribed:  set[str]


def _absolute(name: str) -> str:
    return name if name.startswith('/') else f'/{name}'


def is_present(contract: Contract, graph: GraphSnapshot, vlm_up: bool) -> bool:
    """True if what the contract calls is in the ROS graph now"""
    interface = contract.interface

    if interface.transport == 'action':
        return f'{_absolute(interface.name)}/_action/send_goal' in graph.services

    if interface.transport == 'service':
        return _absolute(interface.name) in graph.services

    if interface.transport == 'watch':
        return interface.success_when.topic in graph.published

    if interface.transport == 'topic':
        sends = _absolute(interface.name) in graph.subscribed
        hears = interface.success_when is None or interface.success_when.topic in graph.published

        return sends and hears

    if interface.transport == 'vlm':
        return vlm_up

    if interface.transport in ('compare', 'graph'):
        return True                 # they run inside the executor

    return False                    # a tree or a code skill: nothing in the graph says it is up
