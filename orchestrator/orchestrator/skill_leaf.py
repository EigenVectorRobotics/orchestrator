import math

from typing import Protocol

from skill_registry import Contract, Registry

from .behavior_tree import DONE, Context, Status, TreeError, TreeNode


class SkillHandle(Protocol):
    """One running call of a skill; an action handle may also keep feedback, a list of samples"""

    message: str
    outputs: dict

    def poll(self) -> Status: ...
    def cancel(self): ...


class DoneHandle:
    """A call that ended before it started"""

    def __init__(self, success: bool, message: str = '', outputs: dict | None = None):
        self.status  = Status.SUCCESS if success else Status.FAILURE
        self.message = message
        self.outputs = outputs or {}

    def poll(self) -> Status:
        return self.status

    def cancel(self):
        pass


class SkillRunner(Protocol):
    def start(self, contract: Contract, inputs: dict) -> SkillHandle: ...


class Transports:
    """The one place a call goes by its transport: a code skill to the code runner (code_skill.py), every other
    transport to the skill runner (ros_skill_runner.py)"""

    def __init__(self, runner: SkillRunner, code=None):
        self.runner = runner
        self.code   = code                  # None: this executor runs no code skills

    def check(self, contract: Contract, path: str):
        """At build time: a call that no runner here can start is a tree error, not a failure later"""
        if contract.interface.transport == 'code' and self.code is None:
            raise TreeError(f'{path}: {contract.name} is a code skill, and this executor runs none')

    def start(self, contract: Contract, inputs: dict, context: Context, path: str) -> SkillHandle:
        if contract.interface.transport == 'code':
            return self.code.start(contract, inputs, context, chain=path)

        return self.runner.start(contract, inputs)


class InputError(ValueError):
    pass


def parse_pose(text: str) -> dict:
    """'x y yaw' or 'x y z yaw' (commas allowed), in metres and radians, in the map frame. z is the storey on a
    multi-storey map: without it the route planner takes the lowest storey"""
    numbers = text.replace(',', ' ').split()

    if len(numbers) not in (3, 4):
        raise InputError(f'a pose is "x y yaw" or "x y z yaw", not "{text}"')

    values = [float(number) for number in numbers]
    pose   = {'x': values[0], 'y': values[1], 'yaw': values[-1], 'frame': 'map'}

    if len(values) == 4:
        pose['z'] = values[2]

    return pose


def _convert(value, port_type: str, port_name: str):
    if not isinstance(value, str):
        return value                            # from the blackboard, already typed

    try:
        if port_type == 'int':
            return int(value)

        if port_type == 'float':
            return float(value)

        if port_type == 'bool':
            if value.lower() not in ('true', 'false'):
                raise InputError(f'{port_name}: a bool is true or false, not {value}')

            return value.lower() == 'true'

        if port_type == 'pose':
            return parse_pose(value)
    except ValueError as error:
        raise InputError(f'{port_name}: {error}') from error

    return value


def _blackboard_key(value) -> str:
    """The key in '{key}', or '': only text holds one, and a code skill's inputs come already typed"""
    return value[1:-1] if isinstance(value, str) and value.startswith('{') and value.endswith('}') else ''


def resolve_inputs(contract: Contract, attributes: dict, blackboard: dict) -> dict:
    """Port values for one call: literals parsed, {key} read from the blackboard, defaults filled"""
    inputs = {}

    for port in contract.inputs:
        if port.name not in attributes:
            if port.required:
                raise InputError(f'missing input {port.name}')

            if port.default is not None:
                inputs[port.name] = port.default

            continue

        value = attributes[port.name]

        if key := _blackboard_key(value):
            if key not in blackboard:
                raise InputError(f'{port.name}: blackboard has no {key}')

            value = blackboard[key]

        value = _convert(value, port.type, port.name)

        if port.type == 'enum' and value not in port.choices:
            raise InputError(f'{port.name}: {value} is not one of {port.choices}')

        inputs[port.name] = value

    return inputs


class SkillLeaf(TreeNode):
    """A tree leaf that runs one skill, of any transport, with the contract's timeout"""

    def __init__(self, contract: Contract, transports: Transports, path: str, attributes: dict, context: Context):
        super().__init__(contract.name, path, attributes, [], context)

        self.contract   = contract
        self.transports = transports
        self.handle     = None
        self.start_time = 0.0
        self.inputs     = {}
        self.outputs    = {}                # the last call's outputs, for whoever asked for the call

    def _tick(self) -> Status:
        if self.handle is None:
            try:
                self.inputs = resolve_inputs(self.contract, self.attributes, self.context.blackboard)
            except InputError as error:
                self.message = str(error)
                return Status.FAILURE

            self.message    = ''
            self.start_time = self.context.clock()
            self.handle     = self.transports.start(self.contract, self.inputs, self.context, self.path)

        status = self.handle.poll()

        if status == Status.RUNNING:
            elapsed_s = self.context.clock() - self.start_time

            if self.contract.timeout_s > 0 and elapsed_s > self.contract.timeout_s:
                self.message = f'timeout after {self.contract.timeout_s:g} s'
                self._finish_call(Status.FAILURE)
                self.handle.cancel()
                self.handle = None

                return Status.FAILURE

            return status

        if status in DONE:
            self.message = self.handle.message
            self.outputs = dict(self.handle.outputs)
            self._write_outputs(self.handle.outputs)
            self._finish_call(status)
            self.handle = None

        return status

    def _finish_call(self, status: Status):
        """Record a finished action call, with its feedback over time; and the failure of a check.

        A condition is recorded only when it fails as the check of the action just before it (the action's contract
        names it in check): its record carries that call as checked_call. Other conditions are polled, so their misses
        are not events.
        """
        record = {
            'time':      self.context.clock(),
            'kind':      self.contract.kind,
            'skill':     self.contract.name,
            'path':      self.path,
            'status':    status.value,
            'inputs':    self.inputs,
            'message':   self.message,
            'outputs':   dict(self.handle.outputs),
            'elapsed_s': round(self.context.clock() - self.start_time, 2),
        }

        if self.contract.kind == 'action':
            record['check'] = self.contract.check
            self.context.record(record, {**record, 'feedback': list(getattr(self.handle, 'feedback', []))})
            return

        if status != Status.FAILURE or not self._is_check_of_last_call():
            return

        checked = self.context.last_call
        record['checks'] = checked['skill']
        self.context.record(record, {**record, 'checked_call': checked})

    def _is_check_of_last_call(self) -> bool:
        last = self.context.history[-1] if self.context.history else None

        return (last is not None and last.get('kind') == 'action' and last.get('status') == Status.SUCCESS.value
                and last.get('check') == self.contract.name and self.context.last_call is not None)

    def _halt(self):
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None

    def describe_inputs(self) -> str:
        """The inputs of the current call, short: a pose as 'name (x, y, yaw deg)'"""
        parts = []

        for port in self.contract.inputs:
            if port.name not in self.inputs:
                continue

            value = self.inputs[port.name]

            if port.type == 'pose':
                key   = _blackboard_key(self.attributes.get(port.name, ''))
                name  = f'{key} ' if key else ''
                value     = f'{name}({value["x"]:.2f}, {value["y"]:.2f}, {math.degrees(value.get("yaw", 0.0)):.0f} deg)'

            parts.append(f'{port.name}={value}')

        return ', '.join(parts)

    def _write_outputs(self, outputs: dict):
        for port in self.contract.outputs:
            key = _blackboard_key(self.attributes.get(port.name, ''))

            if key and port.name in outputs:
                self.context.blackboard[key] = outputs[port.name]


def make_leaf_factory(registry: Registry, runner: SkillRunner, code=None):
    transports = Transports(runner, code)

    def build_leaf(skill_name: str, path: str, attributes: dict, context: Context) -> TreeNode:
        contract = registry.get(skill_name)

        if contract is None:
            raise TreeError(f'{path}: unknown skill {skill_name}')

        transports.check(contract, path)

        return SkillLeaf(contract, transports, path, attributes, context)

    return build_leaf
