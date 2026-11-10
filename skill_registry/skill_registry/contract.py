import re

from string import Formatter
from pathlib import Path
from dataclasses import dataclass, field

import yaml


KINDS         = ('action', 'condition', 'composite')
TRANSPORTS    = ('action', 'service', 'topic', 'watch', 'vlm', 'tree', 'agent', 'compare', 'graph', 'press_tip', 'code')
READ_ONLY     = ('watch', 'vlm', 'compare', 'graph', 'press_tip')         # the transports a condition may use
# watch: read a topic, never publish. agent: an LLM with read-only tools, guided by an agent skill document.
# compare: the input value equals the input equals; nothing leaves the executor.
# graph: each listed topic has at most its allowed publishers. press_tip: where the end effector got to in the last press.
# code: Python that calls other skills through the executor; it runs in the sandbox and reaches no ROS of its own.
MATURITIES    = ('tested', 'wip', 'untested')
PORT_TYPES    = ('string', 'int', 'float', 'bool', 'pose', 'enum')
WHOLE_MESSAGE = '.'                  # Port.field value: the port is the whole message, e.g. a PoseStamped goal
NAME          = re.compile(r'^[a-z][a-z0-9_]{0,62}$')
AGENT_MARK    = '# written by the agent with write_skill; a person may edit it, and then remove this line\n'
# A contract file that starts with AGENT_MARK loads at maturity untested: the agent does not grade its own skills

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
    skill:           str = ''               # agent: the agent skill document, agent_skills/<skill>.md
    capture:         dict = field(default_factory=dict)       # action: frames to keep around the call, e.g. {kind: press}
    frames:          str = ''               # vlm: judge the frames a capture kept (press_capture) instead of the latest one
    compare_question: str = ''              # vlm with frames: asked of [baseline | later frame] side by side
    topics:          dict = field(default_factory=dict)       # graph: topic -> the most publishers it may have
    limits:          dict = field(default_factory=dict)       # press_tip: max_lateral_m, min_depth_m

    def ros_names(self) -> set[str]:
        """Every ROS name the skill sends to or reads: the interface, the watched topic, capture and graph topics"""
        names = {self.name} if self.transport in ('action', 'service', 'topic') else set()

        if self.success_when:
            names.add(self.success_when.topic)

        names |= {value for key, value in self.capture.items() if key.endswith('_topic')}

        return {name for name in names | set(self.topics) if name}

    def ros_types(self) -> set[str]:
        return {ros_type for ros_type in [self.ros_type, self.success_when and self.success_when.ros_type] if ros_type}


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
    check:       str = ''                                   # condition skill that confirms success
    resources:   list[str] = field(default_factory=list)    # exclusive: two live skills never share one
    timeout_s:   float = 0.0
    fallback:    list[str] = field(default_factory=list)    # suggested recovery skills, in order
    needs_cloud: bool = False
    maturity:    str = 'untested'
    source:      str = ''                                   # code the skill wraps
    notes:       str = ''

    def input(self, name: str) -> Port | None:
        return next((port for port in self.inputs if port.name == name), None)

    @property
    def conditions(self) -> list[str]:
        """The condition skills it names: pre, effects and check"""
        return [*self.pre, *self.effects, *([self.check] if self.check else [])]

    @property
    def references(self) -> list[str]:
        return [*self.conditions, *self.fallback]

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

    if not NAME.match(str(name)):
        raise ContractError(f'{name}: a skill name is snake_case (a-z, 0-9, _), at most 63 characters')

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

    if contract.timeout_s <= 0 and (contract.kind == 'action' or interface.transport == 'code'):
        raise ContractError(f'{name}: an action or code skill needs a timeout_s above 0')

    if contract.kind == 'condition' and interface.transport not in READ_ONLY:
        raise ContractError(f'{name}: a condition must only read ({READ_ONLY}), not {interface.transport}')

    if interface.transport == 'watch' and interface.success_when is None:
        raise ContractError(f'{name}: a watch needs success_when')

    if interface.transport == 'vlm' and not interface.question:
        raise ContractError(f'{name}: a vlm skill needs a question')

    if interface.failure_when is not None and interface.transport != 'action':
        raise ContractError(f'{name}: failure_when reads action feedback, so it needs an action skill')

    if interface.capture and interface.capture.get('kind') != 'press':
        raise ContractError(f'{name}: capture kind must be press')

    if interface.frames and (interface.frames != 'press_capture' or interface.transport != 'vlm'):
        raise ContractError(f'{name}: frames must be press_capture, on a vlm skill')

    if interface.transport == 'agent' and not interface.skill:
        raise ContractError(f'{name}: an agent skill needs the skill document it follows')

    if interface.transport == 'compare' and {'value', 'equals'} - {port.name for port in contract.inputs}:
        raise ContractError(f'{name}: a compare skill needs the inputs value and equals')

    for field_name in _format_fields(interface.question) | _format_fields(interface.compare_question):
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
    """A contract file: its name must be the file's name; an agent's file (AGENT_MARK) is at maturity untested"""
    text = Path(path).read_text()
    raw  = yaml.safe_load(text)

    if isinstance(raw, dict) and text.startswith(AGENT_MARK):
        raw['maturity'] = 'untested'

    contract = parse_contract(raw)

    if contract.name != Path(path).stem:
        raise ContractError(f'{path}: the file of skill {contract.name} must be {contract.name}.yaml')

    return contract
