"""The gate list: which calls inside a code skill still ask the operator (gate.yaml beside this file).

A code skill is approved once. After that it runs by itself, so the question is which of its own calls a person
must still see. Anything that can act on the robot asks, unless the operator put it in quiet: a skill the agent
wrote for an interface nobody thought of is therefore asked about, not waved through.
"""

from pathlib import Path
from dataclasses import dataclass, field

import yaml

from .contract import READ_ONLY, Contract
from .deny import PatternList


DEFAULT_PATH = Path(__file__).with_name('gate.yaml')


@dataclass
class GateList:
    quiet: PatternList = field(default_factory=PatternList)

    @classmethod
    def load(cls, path: Path | None = None) -> 'GateList':
        raw = yaml.safe_load(Path(path or DEFAULT_PATH).read_text()) or {}

        return cls(quiet=PatternList(**(raw.get('quiet') or {})))

    def check(self, contract: Contract) -> list[str]:
        """Why this call needs the operator; empty if the call of the code skill covers it"""
        if contract.interface.transport in READ_ONLY:
            return []

        if reasons := self.quiet.check(contract):
            return []                           # the operator said this one is not worth a question

        return [f'{contract.name} can act on the robot ({contract.interface.transport} '
                f'{contract.interface.name or contract.interface.transport})']
