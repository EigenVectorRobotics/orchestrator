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
            self.handle     = self._start_call()

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
            self._write_outputs(self.handle.outputs)
            self._finish_call(status)
            self.handle = None

        return status

    def _start_call(self) -> SkillHandle:
        return self.runner.start(self.contract, self.inputs)

    def _finish_call(self, status: Status):
        """Record a finished action call, with its feedback over time. Conditions are polled, so their answers are not
        events, unless the contract says record (check_reach, which the agent then reads): then every answer is."""
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
            self.context.record(record, {**record, 'feedback': list(getattr(self.handle, 'feedback', []))})
        elif self.contract.record:
            self.context.record(record)

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


class AgentRunner(Protocol):
    def start(self, contract: Contract, inputs: dict, context: Context) -> SkillHandle: ...


class AgentLeaf(SkillLeaf):
    """A leaf that runs an agent skill; it sees the tree's record, and its decision can end the whole tree.

    The handle's outputs carry decision and reason. ask_help ends the agent's part of the task: later calls fail
    at once. abort fails the tree. max_rounds bounds the diagnoses in one task.
    """

    def _start_call(self) -> SkillHandle:
        decisions = [record for record in self.context.history if record.get('skill') == self.contract.name]
        gave_up   = next((record for record in decisions if record.get('decision') == 'ask_help'), None)
        rounds    = sum(1 for record in decisions if record.get('phase') == 'diagnose' and not record.get('rule'))

        if self.runner is None:
            return DoneHandle(False, 'no agent is configured (agent_backend)')

        if gave_up is not None:
            return DoneHandle(False, f'the agent already asked for help: {gave_up.get("reason", "")}')

        if self.inputs.get('phase', 'diagnose') == 'diagnose' and rounds >= int(self.inputs.get('max_rounds', 5)):
            return DoneHandle(False, f'recovery budget spent: {rounds} diagnoses in this task')

        return self.runner.start(self.contract, self.inputs, self.context)

    def _finish_call(self, status: Status):
        outputs  = self.handle.outputs
        decision = outputs.get('decision')

        if not decision:
            return                                      # the agent did not decide: nothing to keep

        self.context.record({
            'time':     self.context.clock(),
            'skill':    self.contract.name,
            'path':     self.path,
            'phase':    self.inputs.get('phase', 'diagnose'),
            'decision': decision,
            'reason':   outputs.get('reason', ''),
            'offset':   outputs.get('offset'),
            'goal':     outputs.get('goal'),
            'left_arm': outputs.get('left_arm'),
            'rule':     bool(outputs.get('rule')),       # decided by the executor's rule, not the agent: outside max_rounds
        })

        checked = self.context.last_call

        if (self.inputs.get('phase') == 'judge' and status == Status.FAILURE and checked is not None
                and checked.get('status') == Status.SUCCESS.value):
            # a failed check of the press: manipulation_recovery then sees the press as checked_call
            record = {'time': self.context.clock(), 'kind': 'condition', 'skill': self.contract.name,
                      'path': self.path, 'status': status.value, 'inputs': self.inputs, 'message': self.message,
                      'outputs': dict(outputs), 'elapsed_s': round(self.context.clock() - self.start_time, 2),
                      'checks': checked['skill']}
            self.context.record(record, {**record, 'checked_call': checked})

        if decision == 'abort':
            self.context.abort(f'{self.contract.name}: {outputs.get("reason", "")}')


def make_leaf_factory(registry: Registry, runner: SkillRunner, agent: AgentRunner | None = None):
    def build_leaf(skill_name: str, path: str, attributes: dict, context: Context) -> TreeNode:
        contract = registry.get(skill_name)

        if contract is None:
            raise TreeError(f'{path}: unknown skill {skill_name}')

        if contract.interface.transport == 'agent':
            return AgentLeaf(contract, agent, path, attributes, context)

        return SkillLeaf(contract, runner, path, attributes, context)

    return build_leaf
