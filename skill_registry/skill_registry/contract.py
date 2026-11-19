"""Parses and checks a skill contract: kinds, transports, ports, watches and config."""

from string import Formatter
from pathlib import Path
from dataclasses import dataclass, field

import yaml


KINDS         = ('action', 'condition', 'composite')
TRANSPORTS    = ('action', 'service', 'topic', 'watch', 'vlm', 'agent', 'compare', 'alive', 'command', 'python')
READ_ONLY     = ('watch', 'vlm', 'compare', 'alive', 'python')   # the transports a condition may use
# watch: read a topic, never publish. agent: one decision from the agent bridge's model or operator, on gathered evidence.
# compare: the input value equals the input equals; nothing leaves the executor.
# alive: each listed topic has a publisher, each service exists, each URL answers. command: run the contract's fixed
# shell command on the executor's machine; success is exit code 0. python: the skill's own skill.py runs every call; a
# condition on it must only read, which its code keeps to.
REQUIRES      = ('services', 'published', 'subscribed')         # interface.requires: what must be up for a python skill
MATURITIES    = ('tested', 'wip', 'untested', 'disabled')   # disabled: kept for later, hidden from the planner, refused in a tree
PORT_TYPES    = ('string', 'int', 'float', 'bool', 'pose', 'enum')
WHOLE_MESSAGE = '.'                  # Port.field value: the port is the whole message, e.g. a PoseStamped goal

# Devices whose loss stops the robot anyway (the NUC runs the tree, the Orin runs the motors).
# A skill on any other device can drop out, so a tree must give it a recovery.
DEFAULT_ESSENTIAL_DEVICES = frozenset({'nuc', 'orin'})


class ContractError(ValueError):
    pass


@dataclass
class Port:
    name:        str
    type:        str
    required:    bool = True
    default:     object = None
    choices:     list[str] = field(default_factory=list)       # only for type 'enum'; the keys of values if given
    values:      dict[str, object] = field(default_factory=dict)   # enum choice -> value sent, e.g. damp -> 1
    field:       str = ''                                       # message field it fills; empty means the port name
    description: str = ''

    @property
    def message_field(self) -> str:
        return self.field or self.name


@dataclass
class Watch:
    """A topic value that ends a skill, or that makes a condition true"""

    topic:    str
    ros_type: str
    field:    str = 'data'           # dotted path into the message, or into its JSON if json is true
    equals:   object = True
    json:     bool = False           # the message is a std_msgs/String that holds JSON
    latched:  bool = False           # the publisher is transient-local, so the last value is kept
    consecutive: int = 1             # the last N messages must all match, to remove one-message errors
    equals_input: str = ''           # a condition: compare with this input instead of equals
    max_age_s: float = 0.0           # a condition: the latest message must be at most this old (0: any age)


@dataclass
class HeldValue:
    """An action feedback field that fails the action once it has held one value for hold_s, e.g. a route that
    stays blocked. A value that can clear by itself needs a hold_s above 0"""

    field:  str                      # dotted path into the action's feedback
    equals: object
    hold_s: float = 0.0


@dataclass
class Interface:
    transport:       str                    # how the executor reaches the capability
    name:            str = ''               # ROS name, or tree ID
    ros_type:        str = ''               # e.g. humanoid_manipulation_interfaces/action/VisualServoingArm
    success_when:    Watch | None = None    # topic skills: the end signal (none: done once sent); watch conditions
    failure_when:    HeldValue | None = None    # action skills: a feedback value that fails the action before its timeout
    fixed:           dict = field(default_factory=dict)        # message fields always sent, e.g. {data: true}
    question:        str = ''               # vlm: may hold {input} fields
    choices:         list[str] = field(default_factory=list)   # vlm: the only answers; empty means the 'choices' input
    success_answer:  str = ''               # vlm: the answer that means success; empty means any answer
    skill:           str = ''               # agent: the skill the bridge answers as: its skill folder's spec.py and prompt.py
    requires:        dict = field(default_factory=dict)       # python: services, published and subscribed topics it needs up
    alive:           dict = field(default_factory=dict)       # alive: topics, services, http (lists)
    command:         str = ''               # command: the fixed shell command; inputs cannot change it


@dataclass
class Contract:
    """What a skill needs, does, and how it fails"""

    name:        str
    kind:        str
    device:      str                 # where the capability runs, not where the executor runs
    interface:   Interface
    description: str = ''
    inputs:      list[Port] = field(default_factory=list)
    outputs:     list[Port] = field(default_factory=list)
    pre:         list[str] = field(default_factory=list)    # condition skills that must hold
    effects:     list[str] = field(default_factory=list)    # condition skills that hold after
    resources:   list[str] = field(default_factory=list)    # exclusive: two live skills never share one
    timeout_s:   float = 0.0
    fallback:    list[str] = field(default_factory=list)    # suggested recovery skills, in order
    needs_cloud: bool = False
    maturity:    str = 'untested'
    source:      str = ''                                   # code the skill wraps
    notes:       str = ''
    record:      bool = False   # a condition: record every answer for the agent, even when it checks no action
    config:      dict = field(default_factory=dict)         # the skill's own settings, read by its skill.py

    def input(self, name: str) -> Port | None:
        return next((port for port in self.inputs if port.name == name), None)

    @property
    def required_inputs(self) -> list[str]:
        return [port.name for port in self.inputs if port.required]


def _parse_ports(raw_ports: list | None, skill_name: str) -> list[Port]:
    ports = [Port(**raw_port) for raw_port in raw_ports or []]

    for port in ports:
        if port.values and not port.choices:
            port.choices = list(port.values)

        if port.type not in PORT_TYPES:
            raise ContractError(f'{skill_name}: port {port.name} has unknown type {port.type}')

        if port.type == 'enum' and not port.choices:
            raise ContractError(f'{skill_name}: enum port {port.name} has no choices')

        if port.values and set(port.values) != set(port.choices):
            raise ContractError(f'{skill_name}: enum port {port.name} needs one value per choice')

    return ports


def _parse_interface(raw: dict) -> Interface:
    raw = dict(raw)

    if raw.get('success_when'):
        raw['success_when'] = Watch(**raw['success_when'])

    if raw.get('failure_when'):
        raw['failure_when'] = HeldValue(**raw['failure_when'])

    return Interface(**raw)


def parse_contract(raw: dict) -> Contract:
    if not isinstance(raw, dict) or 'name' not in raw:
        raise ContractError(f'contract has no name: {raw}')

    name = raw['name']

    try:
        contract = Contract(
            **{
                **raw,
                'interface': _parse_interface(raw['interface']),
                'inputs':    _parse_ports(raw.get('inputs'), name),
                'outputs':   _parse_ports(raw.get('outputs'), name),
            }
        )
    except (KeyError, TypeError) as error:
        raise ContractError(f'{name}: {error}') from error

    interface = contract.interface

    if contract.kind not in KINDS:
        raise ContractError(f'{name}: unknown kind {contract.kind}')

    if interface.transport not in TRANSPORTS:
        raise ContractError(f'{name}: unknown transport {interface.transport}')

    if contract.maturity not in MATURITIES:
        raise ContractError(f'{name}: unknown maturity {contract.maturity}')

    if contract.kind == 'action' and contract.timeout_s <= 0:
        raise ContractError(f'{name}: an action skill needs a timeout_s above 0')

    if contract.kind == 'condition' and interface.transport not in READ_ONLY:
        raise ContractError(f'{name}: a condition must only read ({READ_ONLY}), not {interface.transport}')

    if interface.transport == 'watch' and interface.success_when is None:
        raise ContractError(f'{name}: a watch needs success_when')

    if interface.transport == 'vlm' and not interface.question:
        raise ContractError(f'{name}: a vlm skill needs a question')

    if interface.failure_when is not None and interface.transport != 'action':
        raise ContractError(f'{name}: failure_when reads action feedback, so it needs an action skill')

    if set(interface.requires) - set(REQUIRES):
        raise ContractError(f'{name}: interface.requires takes {REQUIRES}, not {sorted(set(interface.requires) - set(REQUIRES))}')

    if interface.transport == 'alive' and not any(interface.alive.get(key) for key in ('topics', 'services', 'http')):
        raise ContractError(f'{name}: an alive skill needs alive.topics, alive.services or alive.http')

    if interface.transport == 'command' and not interface.command.strip():
        raise ContractError(f'{name}: a command skill needs interface.command')


    watch = interface.success_when

    if watch is not None and watch.equals_input and contract.input(watch.equals_input) is None:
        raise ContractError(f'{name}: success_when.equals_input names no input: {watch.equals_input}')

    if watch is not None and isinstance(watch.equals, str) and _format_fields(watch.equals):
        raise ContractError(f'{name}: success_when.equals is compared as it is; to compare with an input, '
                            f'use equals_input: {watch.equals.strip("{}")}')

    if contract.record and contract.kind != 'condition':
        raise ContractError(f'{name}: record is for conditions; actions are always recorded')

    if interface.transport == 'agent' and not interface.skill:
        raise ContractError(f'{name}: an agent skill needs the skill the bridge answers as')

    if interface.transport == 'compare' and {'value', 'equals'} - {port.name for port in contract.inputs}:
        raise ContractError(f'{name}: a compare skill needs the inputs value and equals')

    for field_name in _format_fields(interface.question):
        if contract.input(field_name) is None:
            raise ContractError(f'{name}: the question uses {{{field_name}}}, which is not an input')

    if interface.success_answer and interface.choices and interface.success_answer not in interface.choices:
        raise ContractError(f'{name}: success_answer {interface.success_answer} is not one of {interface.choices}')

    return contract


def _format_fields(text: str) -> set[str]:
    """The {name} fields in a str.format template"""
    try:
        return {field_name.split('.')[0].split('[')[0] for _, field_name, _, _ in Formatter().parse(text) if field_name}
    except ValueError as error:
        raise ContractError(f'bad question template {text!r}: {error}') from error


def load_contract(path: Path) -> Contract:
    with open(path) as file:
        return parse_contract(yaml.safe_load(file))
