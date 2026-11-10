import os
import time
import logging
import threading

from pathlib import Path
from dataclasses import dataclass

import yaml

from .deny import DenyList
from .contract import Contract, ContractError, load_contract, parse_contract


def agent_dir() -> Path:
    """The agent's own folder: contracts/ (write_skill), agent_skills/ and docs/; the package has no skills"""
    return Path(os.environ.get('EIGEN_VECTOR_WS', Path.home())) / 'agent'


def agent_contracts_dir() -> Path:
    return agent_dir() / 'contracts'


def agent_skills_dir() -> Path:
    return agent_dir() / 'agent_skills'


def agent_code_dir() -> Path:
    return agent_dir() / 'code'


def code_file(name: str, code_dir=None) -> Path:
    """Where the Python of a code skill lives; the runner and write_skill must agree on it"""
    return Path(code_dir) / f'{name}.py' if code_dir else agent_code_dir() / f'{name}.py'


def missing_folder(directory) -> str:
    """'' if the folder is there; else why no skill loads, e.g. a typo in contracts_directory"""
    return '' if Path(directory).is_dir() else f'no folder {directory}: no skill loads until it is made'


class Registry:
    """All known skill contracts, and which of them are live now"""

    def __init__(self, contracts: list[Contract] | None = None, deny: DenyList | None = None):
        self._contracts: dict[str, Contract] = {}
        self._last_seen: dict[str, float]    = {}   # skill name -> monotonic time of last announce
        self.deny                            = deny if deny is not None else DenyList.load()
        self.denied:     dict[str, list[str]] = {}  # skill name -> why it was left out
        self._file_times: dict[str, float]    = {}  # refresh: contract file -> the mtime it was last read at
        self._refreshing                      = threading.Lock()

        for contract in contracts or []:
            self.add(contract)

    @classmethod
    def from_directory(cls, directory: Path, deny: DenyList | None = None) -> 'Registry':
        """The contracts in directory, without those the deny list refuses (see registry.denied); none if it is empty"""
        paths    = sorted(Path(directory).glob('*.yaml'))
        registry = cls([load_contract(path) for path in paths], deny)
        registry._drop_dependents_of_denied()
        registry.check_references()
        registry._file_times = {path.name: path.stat().st_mtime for path in paths}

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
        and the files that did not load with why. Nothing is removed"""
        with self._refreshing:
            pending, errors = {}, {}

            for path in sorted(Path(directory).glob('*.yaml')):
                mtime = path.stat().st_mtime

                if self._file_times.get(path.name) == mtime:
                    continue

                self._file_times[path.name] = mtime

                try:
                    contract = load_contract(path)
                except (ContractError, OSError, ValueError, TypeError, yaml.YAMLError) as error:   # e.g. a half-saved edit
                    errors[path.name] = str(error)
                    continue

                if self.get(contract.name) != contract:
                    pending[path.name] = contract

            changed = self._admit_all(pending, check, errors)

        for file_name, error in errors.items():
            logging.getLogger('skill_registry').warning(f'{file_name} not loaded: {error}')

        return changed, errors

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

    # ========== Online discovery ==========

    def announce(self, raw: dict, now: float | None = None) -> Contract:
        """A skill server says it is up. New skills join the registry, unless the deny list refuses them"""
        contract = parse_contract(raw)

        if contract.name not in self and not self.add(contract):
            raise ContractError(f'{contract.name} is denied: {"; ".join(self.denied[contract.name])}')

        self._last_seen[contract.name] = time.monotonic() if now is None else now

        return self._contracts[contract.name]

    def is_live(self, name: str, max_age_s: float, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now

        return name in self._last_seen and now - self._last_seen[name] <= max_age_s

    def live(self, max_age_s: float, now: float | None = None) -> list[Contract]:
        now = time.monotonic() if now is None else now

        return [
            self._contracts[name]
            for name, seen in self._last_seen.items()
            if now - seen <= max_age_s
        ]


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

    if interface.transport in ('agent', 'compare', 'graph', 'press_tip'):
        return True                 # they run inside the executor

    return False                    # trees are live only when announced
