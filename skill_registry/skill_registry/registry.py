import time
import logging

from pathlib import Path
from dataclasses import dataclass

from .deny import DenyList
from .contract import Contract, ContractError, load_contract, parse_contract


class Registry:
    """All known skill contracts, and which of them are live now"""

    def __init__(self, contracts: list[Contract] | None = None, deny: DenyList | None = None):
        self._contracts: dict[str, Contract] = {}
        self._last_seen: dict[str, float]    = {}   # skill name -> monotonic time of last announce
        self.deny                            = deny if deny is not None else DenyList.load()
        self.denied:     dict[str, list[str]] = {}  # skill name -> why it was left out

        for contract in contracts or []:
            self.add(contract)

    @classmethod
    def from_directory(cls, directory: Path, deny: DenyList | None = None) -> 'Registry':
        """The contracts in directory, without those the deny list refuses (see registry.denied)"""
        paths = sorted(Path(directory).glob('*.yaml'))

        if not paths:
            raise ContractError(f'no contracts (*.yaml) in {directory}')

        registry = cls([load_contract(path) for path in paths], deny)
        registry._drop_dependents_of_denied()
        registry.check_references()

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
        return True

    def _drop_dependents_of_denied(self):
        """A skill that names a denied skill (pre, effects, check, fallback) is left out too, until none is left"""
        while True:
            needs = {contract.name: sorted(set(_references(contract)) & set(self.denied)) for contract in self}
            needs = {name: denied for name, denied in needs.items() if denied}

            if not needs:
                return

            for name, denied in needs.items():
                del self._contracts[name]
                self.denied[name] = [f'it needs the denied skill {skill}' for skill in denied]
                logging.getLogger('skill_registry').warning(f'{name} not loaded: {"; ".join(self.denied[name])}')

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
            for condition in [*contract.pre, *contract.effects, *([contract.check] if contract.check else [])]:
                target = self.get(condition)

                if target is None:
                    raise ContractError(f'{contract.name}: unknown condition {condition}')

                if target.kind != 'condition':
                    raise ContractError(f'{contract.name}: {condition} is a {target.kind}, not a condition')

            for fallback in contract.fallback:
                if fallback not in self:
                    raise ContractError(f'{contract.name}: unknown fallback {fallback}')

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


def _references(contract: Contract) -> list[str]:
    return [*contract.pre, *contract.effects, *([contract.check] if contract.check else []), *contract.fallback]


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
