"""The operator's lists of skills, by ROS name or type: the deny list (never), and the gate list (ask again).

Both are files beside this one that the agent never writes, so a contract cannot talk its way past them.
"""

from fnmatch import fnmatch
from pathlib import Path
from dataclasses import dataclass, field

import yaml

from .contract import Contract


DEFAULT_PATH = Path(__file__).with_name('deny.yaml')


@dataclass
class PatternList:
    """ROS names and types the operator picked out, as fnmatch patterns"""

    names: list[str] = field(default_factory=list)          # fnmatch patterns for ROS names
    types: list[str] = field(default_factory=list)          # fnmatch patterns for ROS types

    PATH = DEFAULT_PATH
    WORD = 'listed'                                         # how a match reads in a reason

    @classmethod
    def load(cls, path: Path | None = None):
        return cls(**(yaml.safe_load(Path(path or cls.PATH).read_text()) or {}))

    def check(self, contract: Contract) -> list[str]:
        """Why the contract matches; empty if it does not"""
        reasons  = [f'{name} matches the {self.WORD} name {pattern}'
                    for name in sorted(contract.interface.ros_names()) for pattern in self.names if _matches(name, pattern)]
        reasons += [f'{ros_type} matches the {self.WORD} type {pattern}'
                    for ros_type in sorted(contract.interface.ros_types()) for pattern in self.types if fnmatch(ros_type, pattern)]

        return reasons


@dataclass
class DenyList(PatternList):
    values:  dict[str, dict[str, list]] = field(default_factory=dict)   # name -> field -> the values allowed
    modules: list[str] = field(default_factory=list)          # Python modules the sandbox blocks

    WORD = 'denied'

    def check(self, contract: Contract) -> list[str]:
        """Why the contract is denied; empty if it is not"""
        names   = sorted(contract.interface.ros_names())
        reasons = super().check(contract)

        for name, limits in self.values.items():
            if any(_matches(own, name) for own in names):
                reasons += [reason for field_name, allowed in limits.items()
                            for reason in _value_reasons(contract, field_name, allowed)]

        return reasons


def _matches(name: str, pattern: str) -> bool:
    """A ROS name with or without its leading slash"""
    return fnmatch(name.lstrip('/'), pattern.lstrip('/'))


def _value_reasons(contract: Contract, field_name: str, allowed: list) -> list[str]:
    name   = contract.interface.name
    fixed  = contract.interface.fixed
    ports  = [port for port in contract.inputs if port.message_field == field_name]

    if field_name in fixed and fixed[field_name] not in allowed:
        return [f'{name} {field_name} is fixed to {fixed[field_name]}; allowed: {allowed}']

    if field_name not in fixed and not ports:
        return [f'{name} must set {field_name} to one of {allowed}']

    reasons = []

    for port in ports:
        if port.type != 'enum':
            reasons.append(f'{name} {field_name} comes from the input {port.name}, which must be an enum of {allowed}')
        else:
            sent = list(port.values.values()) if port.values else port.choices
            reasons += [f'{name} {field_name} can be {value}; allowed: {allowed}' for value in sent if value not in allowed]

    return reasons
