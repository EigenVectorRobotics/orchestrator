"""All contracts from the skill folders, and which of them are live in the ROS graph."""

import importlib.util

from pathlib import Path
from dataclasses import dataclass

from .contract import Contract, ContractError, load_contract


class Registry:
    """All known skill contracts"""

    def __init__(self, contracts: list[Contract] | None = None):
        self._contracts: dict[str, Contract] = {}

        for contract in contracts or []:
            self.add(contract)

    @classmethod
    def from_directory(cls, directory: Path) -> 'Registry':
        return cls.from_directories([directory])

    @classmethod
    def from_directories(cls, directories: list[Path]) -> 'Registry':
        """Every contract in the directories: a skill folder's contract.yaml, or a flat <name>.yaml"""
        paths = [path for directory in directories for path in contract_paths(Path(directory))]

        if not paths:
            raise ContractError(f'no contracts (*/contract.yaml or *.yaml) in {[str(directory) for directory in directories]}')

        registry = cls([load_contract(path) for path in paths])
        registry.check_references()

        return registry

    def add(self, contract: Contract):
        if contract.name in self._contracts:
            raise ContractError(f'duplicate skill {contract.name}')

        self._contracts[contract.name] = contract

    def get(self, name: str) -> Contract | None:
        return self._contracts.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._contracts

    def __iter__(self):
        return iter(self._contracts.values())

    def __len__(self) -> int:
        return len(self._contracts)

    def check_references(self):
        """Every pre, effect and fallback names a known skill of the right kind"""
        for contract in self:
            for condition in [*contract.pre, *contract.effects]:
                target = self.get(condition)

                if target is None:
                    raise ContractError(f'{contract.name}: unknown condition {condition}')

                if target.kind != 'condition':
                    raise ContractError(f'{contract.name}: {condition} is a {target.kind}, not a condition')

            for fallback in contract.fallback:
                if fallback not in self:
                    raise ContractError(f'{contract.name}: unknown fallback {fallback}')


DEFAULT_SKILL_PACKAGES = ['g1_skills']


def contract_paths(directory: Path) -> list[Path]:
    return sorted([*directory.glob('*/contract.yaml'), *directory.glob('*.yaml')])


def contract_directories(skill_packages: list[str] | None = None, directory: str = '') -> list[Path]:
    """Where the contracts are: directory when given, else each skill package's folder (one folder per skill)"""
    if directory:
        return [Path(directory)]

    found = []

    for package in skill_packages or DEFAULT_SKILL_PACKAGES:
        spec = importlib.util.find_spec(package)

        if spec is None or not spec.submodule_search_locations:
            raise ContractError(f'no skill package {package}: build it, or give contracts_directory')

        found.append(Path(next(iter(spec.submodule_search_locations))))

    return found


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

    if interface.transport in ('agent', 'compare', 'alive', 'command'):
        return True                 # they run inside the executor; alive and command must work while the stacks are down

    if interface.transport == 'python':
        need = interface.requires

        return (all(_absolute(name) in graph.services for name in need.get('services', []))
                and all(name in graph.published for name in need.get('published', []))
                and all(_absolute(name) in graph.subscribed for name in need.get('subscribed', [])))

    return False                    # no other transport passes the contract check (TRANSPORTS)
