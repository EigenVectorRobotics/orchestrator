"""write_skill: the agent writes a contract (docs/skill-discovery.md). It is checked (the schema, the deny list, the
references, the ROS types), saved in the agent's skill folder with AGENT_MARK, and loaded through Registry.refresh,
the same way in as a file that changes on disk, so the next step can run it.

The agent does not grade its own skills: a file with the mark loads at maturity untested. It may write a new version
of a skill it wrote (the old file goes to old/), never one that a person wrote. This module needs no ROS: the
executor gives it the ROS type check and what to do with a loaded contract.
"""

from typing import Callable
from pathlib import Path

import yaml

from skill_registry import Contract, ContractError, Registry, parse_contract
from skill_registry.contract import AGENT_MARK


class SkillWriter:
    def __init__(self, registry: Registry, directory: Path, check_types: Callable[[Contract], list[str]],
                 loaded: Callable[[Contract], None]):
        self.registry    = registry
        self.directory   = Path(directory)
        self.check_types = check_types            # contract -> what does not match its ROS types
        self.loaded      = loaded                 # e.g. the runner makes its clients for the new skill

    def write(self, text: str) -> dict:
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

        path     = self.directory / f'{contract.name}.yaml'
        replaced = path.exists()

        if replaced and not path.read_text().startswith(AGENT_MARK):
            return _refused(f'{contract.name} was written by a person; choose another name')

        errors = self.registry.admit_errors(contract) or self.type_errors(contract)

        if errors:
            return _refused('; '.join(errors))

        self._save(path, raw)
        changed, errors = self.registry.refresh(self.directory, self.type_errors)

        for name in changed:
            self.loaded(self.registry.get(name))

        if path.name in errors:
            return _refused(errors[path.name])

        return {'ok': True, 'message': f'{contract.name} {"replaced" if replaced else "added"}, maturity untested',
                'path': str(path)}

    def type_errors(self, contract: Contract) -> list[str]:
        try:
            return self.check_types(contract)
        except ModuleNotFoundError as error:
            return [f'{contract.interface.ros_type}: the package {error.name} is not built on this machine']
        except Exception as error:                  # a check, not a crash: any failure here refuses the contract
            return [f'{contract.interface.ros_type}: {error!r}']

    def refresh(self) -> list[str]:
        """From the executor's timer: files that changed on disk (a person's edit) load the same way"""
        changed, _ = self.registry.refresh(self.directory, self.type_errors)

        for name in changed:
            self.loaded(self.registry.get(name))

        return changed

    def _save(self, path: Path, raw: dict):
        self.directory.mkdir(parents=True, exist_ok=True)

        if path.exists():
            old = self.directory / 'old'
            old.mkdir(exist_ok=True)
            numbers = [int(file.suffixes[-2][1:]) for file in old.glob(f'{path.stem}.*.yaml') if file.suffixes[-2][1:].isdigit()]
            path.rename(old / f'{path.stem}.{max(numbers, default=0) + 1}.yaml')

        path.write_text(AGENT_MARK + yaml.safe_dump(raw, sort_keys=False))


def _refused(message: str) -> dict:
    return {'ok': False, 'message': f'not saved: {message}'}
