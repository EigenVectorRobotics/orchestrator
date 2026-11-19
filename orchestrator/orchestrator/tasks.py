"""Tasks and their steps, for the executor: one task is one run, and each step is one tree on the task's Context.

A task stays open between steps (docs/task-mode.md), and run_skill runs one skill as a step from another thread
(the task agent's) while the executor's timer ticks it. Steps share the task's blackboard and history, so an output of one step can be an input
of the next.

The trace events: task_started and task_finished(end, steps, reason) around the steps; started (with the step number) and
finished, halted or error for each step. This module needs no ROS.
"""

import json
import threading

from typing import Callable
from xml.sax.saxutils import quoteattr
from dataclasses import dataclass, field

from skill_registry import Contract, Issue, Registry, TreeValidator, ValidatorConfig

from .behavior_tree import BehaviorTree, Context, Status, TreeError


def parse_task_request(text: str) -> tuple[str, bool]:
    """A message on /orchestrator/task: a plain sentence, or JSON {"task": "...", "blind": true}"""
    try:
        request = json.loads(text)
        return str(request['task']), bool(request.get('blind', False))
    except (json.JSONDecodeError, TypeError, KeyError):
        return text, False


@dataclass
class Task:
    """One task: one run for the recorder, one Context (blackboard, history, scratch) for all its steps"""
    run_id:  str
    name:    str
    context: Context
    blind:   bool
    steps:   int = 0


@dataclass
class StepResult:
    status:  str                                  # success, failure, halted or error
    message: str = ''
    outputs: dict = field(default_factory=dict)   # the skill's outputs by blackboard key (step<n>_<port>), for {key} later
    failure: dict | None = None                   # the failed call or check of this step, with its feedback


@dataclass
class _Step:
    tree:    BehaviorTree
    xml:     str
    outputs: list[str] = field(default_factory=list)   # run_skill: the blackboard keys of its outputs
    subgoal: str = ''                                  # run_skill: the subgoal of the task plan it is for
    done:    threading.Event = field(default_factory=threading.Event)
    result:  StepResult | None = None
    failure: object = None                        # the task's last failure before the step, to see a new one


class Tasks:
    def __init__(self, registry: Registry, leaf_factory, tracer, publish_status: Callable[[str, str | None], None],
                 essential_devices: set[str], make_scratch: Callable[[str], str | None] = lambda run_id: None,
                 on_error: Callable[[str], None] = lambda text: None):
        self.registry          = registry
        self.make_scratch      = make_scratch            # the run's scratch folder, made when its task starts
        self.leaf_factory      = leaf_factory            # make_leaf_factory(registry, runner, code)
        self.tracer            = tracer                  # emit(kind, text, level, **data) and fields
        self.publish_status    = publish_status
        self.essential_devices = essential_devices
        self.on_error          = on_error
        self.task              = None
        self.step              = None                    # the step that runs or waits for the next tick
        self._lock             = threading.Lock()        # run_skill comes from the agent's thread, ticks from the timer

    # ========== In ==========

    def start_task(self, name: str, context: Context, blind: bool = False) -> Task:
        """A task that stays open between steps until end_task"""
        task = Task(context.run_id, name, context, blind)

        with self._lock:
            self._stop('halted', 'halted for a new task')
            self._start_task(task)

        return task

    def run_skill(self, name: str, inputs: dict, should_stop: Callable[[], bool] = lambda: False, subgoal: str = '') -> StepResult:
        """Task mode, from the task agent's thread: run one skill as the next step, and wait for its end. A string
        input goes into the tree as written (a literal, or {key} for the blackboard); any other value goes on the
        blackboard first. Inputs and outputs that go by the blackboard get the key step<n>_<port>"""
        with self._lock:
            task = self.task

            if task is None:
                return StepResult('error', 'no task is open')

            if self.step is not None:
                return StepResult('error', 'a step is still running')

        contract = self.registry.get(name)

        if contract is None:
            return StepResult('error', f'no skill {name}')

        prefix     = f'step{task.steps + 1}'
        attributes = {}
        outputs    = {port.name: f'{prefix}_{port.name}' for port in contract.outputs}

        for port, value in inputs.items():
            if isinstance(value, str):
                attributes[port] = value
            else:
                task.context.blackboard[f'{prefix}_{port}'] = value
                attributes[port]                           = f'{{{prefix}_{port}}}'

        attributes |= {port: f'{{{key}}}' for port, key in outputs.items()}
        leaf        = ' '.join(f'{port}={quoteattr(str(value))}' for port, value in attributes.items())
        xml_text    = f'<root BTCPP_format="4"><BehaviorTree ID="{prefix}_{name}"><{name} {leaf}/></BehaviorTree></root>'
        config      = ValidatorConfig(essential_devices=self.essential_devices, blind=task.blind,
                                      known_keys=set(task.context.blackboard), require_fallback=False)  # the agent recovers

        errors = self._validate(xml_text, config, 'step')

        if errors:
            return StepResult('error', 'the step does not validate: '
                                       + '; '.join(f'{issue.path}: {issue.message}' for issue in errors))

        try:
            step = _Step(BehaviorTree(xml_text, self.leaf_factory, task.context), xml_text, list(outputs.values()),
                         subgoal, failure=task.context.last_failure)
        except (TreeError, ValueError) as error:
            return StepResult('error', f'the step does not build: {error}')

        with self._lock:
            if self.task is not task or self.step is not None:
                return StepResult('error', 'the task changed before the step started')

            self._start_step(step)

        while not step.done.wait(0.1):
            if should_stop():
                self.cancel_step(step, 'the agent was stopped')

        return step.result

    def end_task(self, task: Task, end: str, reason: str = ''):
        """Task mode: done or gave_up, from the task agent; a running step is halted first. Nothing if this task
        is no longer open (a new task replaced it)"""
        with self._lock:
            if self.task is task:
                self._stop(end, f'the task ended ({end}) during the step', reason)

    def cancel(self):
        with self._lock:
            self._stop('cancelled', 'cancelled')

    def cancel_step(self, step: _Step, reason: str):
        """Halt this step, if it still runs"""
        with self._lock:
            if self.step is step:
                self.step.tree.halt()
                self._end_step('halted', f'{self.step.tree.id}: {reason}', 'warn', StepResult('halted', reason))

    # ========== Ticks ==========

    def tick(self):
        with self._lock:
            if self.step is None:
                return

            tree = self.step.tree

            try:
                status = tree.tick()
            except Exception as error:                  # a skill or runner bug must not take the executor down
                tree.halt()
                self.on_error(f'tick failed: {error!r}')
                self._end_step('error', f'{tree.id} stopped by an internal error: {error!r}', 'error',
                               StepResult('error', repr(error)))
                return

            if status == Status.RUNNING:
                return

            context = self.task.context
            level   = 'info' if status == Status.SUCCESS else 'error'
            text    = f'{tree.id}: {status.value}'
            outputs = {port: context.blackboard[port] for port in self.step.outputs if port in context.blackboard}
            failure = context.last_failure if context.last_failure is not self.step.failure else None

            self._end_step('finished', text, level, StepResult(status.value, text, outputs, failure),
                           status=status.value)

    # ========== Inside, under the lock ==========

    def _start_task(self, task: Task):
        task.context.scratch_dir = self.make_scratch(task.run_id)
        self.task                = task
        self.tracer.fields = {'run_id': task.run_id}
        self.tracer.emit('task_started', f'task {task.run_id}: {task.name}', name=task.name, blind=task.blind,
                         scratch=task.context.scratch_dir, places=dict(task.context.blackboard))

    def _start_step(self, step: _Step):
        self.task.steps += 1
        self.step        = step
        self.tracer.fields.update(tree_id=step.tree.id, step=self.task.steps)
        self.tracer.emit('started', f'running {step.tree.id}' + (' (blind)' if self.task.blind else ''),
                         tree=step.xml, blind=self.task.blind, subgoal=step.subgoal)
        self.publish_status('running', step.tree.id)

    def _end_step(self, kind: str, text: str, level: str, result: StepResult, **data):
        """finished, halted or error; the task stays open"""
        step, self.step = self.step, None
        self.tracer.emit(kind, text, level=level, **data)
        self.tracer.fields = {'run_id': self.task.run_id}
        step.result        = result
        step.done.set()

        self.publish_status('waiting', self.task.name)             # the task is open: the agent decides the next step

    def _end_task(self, end: str, level: str, reason: str = ''):
        """How the task ended: success, failure, halted, cancelled, error, done or gave_up"""
        task, self.task = self.task, None
        self.tracer.emit('task_finished', f'task {task.run_id}: {end}' + (f': {reason}' if reason else ''), level=level,
                         end=end, steps=task.steps, reason=reason)
        self.tracer.fields = {}
        self.publish_status(end, task.name)

    def _stop(self, end: str, text: str, reason: str = ''):
        """Halt the running step, if any, and end the open task, if any, as end"""
        if self.task is None:
            return

        if self.step is not None:
            self.step.tree.halt()
            self._end_step('halted', f'{self.step.tree.id}: {text}', 'warn', StepResult('halted', text))

        self._end_task(end, 'warn' if end in ('halted', 'cancelled') else 'info', reason)

    def _validate(self, xml_text: str, config: ValidatorConfig, what: str) -> list[Issue]:
        """The errors that stop the tree, [] when it validates. The caller gives them back to whoever sent the
        tree: an agent then reads WHY its step was refused, not only that something is in the trace"""
        issues = TreeValidator(self.registry, config).validate(xml_text)
        errors = [issue for issue in issues if issue.level == 'error']

        for issue in issues:
            if issue.code == 'maturity':
                continue                                # every skill is wip for now: not worth a trace line

            self.tracer.emit('validation', f'{issue.path}: {issue.message}', level='error' if issue.level == 'error' else 'warn')

        if errors:
            self.reject(f'{what} rejected: {len(errors)} errors')

        return errors

    def reject(self, text: str) -> bool:
        self.tracer.emit('rejected', text, level='error')
        self.publish_status('rejected', None)
        return False


# ========== The catalog, as the agent reads it ==========

def describe_contract(contract: Contract, essential_devices: set[str], stale: str = '') -> str:
    def describe_port(port) -> str:
        text = f'{port.name}: {port.type}'

        if port.choices:
            text += f' one of {port.choices}'

        if not port.required:
            text += f' (optional, default {port.default})'

        if port.description:
            text += f' - {port.description}'

        return text

    lines = [f'## {contract.name} ({contract.kind})', contract.description]

    if contract.inputs:
        lines.append('inputs: ' + '; '.join(describe_port(port) for port in contract.inputs))

    if contract.outputs:
        lines.append('outputs: ' + '; '.join(describe_port(port) for port in contract.outputs))

    if contract.pre:
        lines.append(f'needs before: {", ".join(contract.pre)}')

    if contract.check:
        lines.append(f'check after: {contract.check}')

    if contract.resources:
        lines.append(f'resources: {", ".join(contract.resources)}')

    if contract.timeout_s:
        lines.append(f'timeout: {contract.timeout_s:g} s')

    if contract.device not in essential_devices:
        lines.append(f'runs on {contract.device}: can drop out')

    if contract.needs_cloud:
        lines.append('needs the cloud')

    lines.append(f'maturity: {contract.maturity}')

    if stale:                     # its own line, as 'needs the cloud' is: maturity is the agent's word, not ours
        lines.append(f'stale, test it before you trust it: {stale}')

    return '\n'.join(line for line in lines if line)


def describe_catalog(registry: Registry, essential_devices: set[str], stale: dict[str, str] | None = None) -> str:
    """stale: skill -> why its code no longer proves it (skill_stamps.Stamps.reasons), read once for the catalog"""
    return '\n\n'.join(
        describe_contract(contract, essential_devices, (stale or {}).get(contract.name, ''))
        for contract in sorted(registry, key=lambda contract: contract.name)
    )
