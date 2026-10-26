import math

from typing import Protocol

from skill_registry import Contract, Registry

from .behavior_tree import DONE, Context, Status, TreeError, TreeNode


class SkillHandle(Protocol):
    """One running call of a skill"""

    message: str
    outputs: dict

    def poll(self) -> Status: ...
    def cancel(self): ...


class SkillRunner(Protocol):
    def start(self, contract: Contract, inputs: dict) -> SkillHandle: ...


class InputError(ValueError):
    pass


def parse_pose(text: str) -> dict:
    """'x y yaw' or 'x,y,yaw', in metres and radians, in the map frame"""
    numbers = text.replace(',', ' ').split()

    if len(numbers) != 3:
        raise InputError(f'a pose is "x y yaw", not "{text}"')

    x, y, yaw = (float(number) for number in numbers)

    return {'x': x, 'y': y, 'yaw': yaw, 'frame': 'map'}


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

        if value.startswith('{') and value.endswith('}'):
            key = value[1:-1]

            if key not in blackboard:
                raise InputError(f'{port.name}: blackboard has no {key}')

            value = blackboard[key]

        value = _convert(value, port.type, port.name)

        if port.type == 'enum' and value not in port.choices:
            raise InputError(f'{port.name}: {value} is not one of {port.choices}')

        inputs[port.name] = value

    return inputs


class SkillLeaf(TreeNode):
    """A tree leaf that runs one skill through the runner, with the contract's timeout"""

    def __init__(self, contract: Contract, runner: SkillRunner, path: str, attributes: dict, context: Context):
        super().__init__(contract.name, path, attributes, [], context)

        self.contract   = contract
        self.runner     = runner
        self.handle     = None
        self.start_time = 0.0
        self.inputs     = {}

    def _tick(self) -> Status:
        if self.handle is None:
            try:
                self.inputs = resolve_inputs(self.contract, self.attributes, self.context.blackboard)
            except InputError as error:
                self.message = str(error)
                return Status.FAILURE

            self.message    = ''
            self.start_time = self.context.clock()
            self.handle     = self.runner.start(self.contract, self.inputs)

        status = self.handle.poll()

        if status == Status.RUNNING:
            elapsed_s = self.context.clock() - self.start_time

            if self.contract.timeout_s > 0 and elapsed_s > self.contract.timeout_s:
                self.handle.cancel()
                self.handle  = None
                self.message = f'timeout after {self.contract.timeout_s:g} s'

                return Status.FAILURE

            return status

        if status in DONE:
            self.message = self.handle.message
            self._write_outputs(self.handle.outputs)
            self.handle = None

        return status

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
                attribute = self.attributes.get(port.name, '')
                name      = attribute[1:-1] + ' ' if attribute.startswith('{') else ''
                value     = f'{name}({value["x"]:.2f}, {value["y"]:.2f}, {math.degrees(value.get("yaw", 0.0)):.0f} deg)'

            parts.append(f'{port.name}={value}')

        return ', '.join(parts)

    def _write_outputs(self, outputs: dict):
        for port in self.contract.outputs:
            target = self.attributes.get(port.name, '')

            if target.startswith('{') and target.endswith('}') and port.name in outputs:
                self.context.blackboard[target[1:-1]] = outputs[port.name]


def make_leaf_factory(registry: Registry, runner: SkillRunner):
    def build_leaf(skill_name: str, path: str, attributes: dict, context: Context) -> TreeNode:
        contract = registry.get(skill_name)

        if contract is None:
            raise TreeError(f'{path}: unknown skill {skill_name}')

        return SkillLeaf(contract, runner, path, attributes, context)

    return build_leaf
