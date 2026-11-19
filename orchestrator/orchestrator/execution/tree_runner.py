import uuid

from typing import Callable, Protocol

from skill_registry import Registry, TreeValidator, ValidatorConfig

from ..tree.leaf import SkillLeaf, SkillRunner, make_leaf_factory
from ..tree.engine import BehaviorTree, BTContext, Status, TreeError


PREFLIGHT_TREE = 'preflight'        # the ID of skill_registry/preflight/preflight.xml


def preflight_gate(tree_id: str, passed: bool, skip: bool, required: bool) -> tuple[bool, str]:
    """Whether a tree may run, and what to trace: a task tree needs a preflight that passed since the launch, unless
    preflight is not required (require_preflight false) or the sender skips it on purpose"""
    if tree_id == PREFLIGHT_TREE or not required or passed:
        return True, ''

    if skip:
        return True, f'{tree_id} runs without a passed preflight (skip_preflight)'

    return False, (f'tree refused: no preflight has passed since the launch. Send the task to the planner, which runs it '
                   f'first, or send_tree --skip-preflight to run {tree_id} without it')


class Trace(Protocol):
    fields: dict

    def emit(self, kind: str, text: str, level: str = 'info', **data): ...


class Logger(Protocol):
    def error(self, message: str): ...


class TreeRunner:
    """Runs one tree at a time: validates it, applies the recorder and preflight gates, ticks it, and traces each step.
    It knows no ROS: the node feeds it trees and ticks, and publishes what it reports"""

    def __init__(self, registry: Registry, places: dict, essential_devices: set[str], runner: SkillRunner, trace: Trace,
                 on_status: Callable[[str, str | None], None], logger: Logger, require_recorder: bool = False,
                 recorder_ready: Callable[[], tuple[bool, str]] = lambda: (True, ''), require_preflight: bool = False):
        self.registry          = registry
        self.places            = places
        self.essential_devices = essential_devices
        self.runner            = runner
        self.trace             = trace
        self.on_status         = on_status          # (state, ID of the tree running or None)
        self.logger            = logger
        self.require_recorder  = require_recorder   # true: no tree runs unrecorded
        self.recorder_ready    = recorder_ready
        self.require_preflight = require_preflight  # true: no task tree before a passed preflight
        self.preflight_passed  = False              # the last preflight run since the launch succeeded
        self.tree: BehaviorTree | None = None

    # ========== Trees in ==========

    def run(self, xml_text: str, blind: bool = False, skip: bool = False) -> bool:
        """Start xml_text in place of the running tree, or refuse it; skip runs a task tree without a passed preflight"""
        if refusal := self._recorder_refusal() or self._validation_refusal(xml_text, blind):
            return self._reject(refusal)

        run_id  = uuid.uuid4().hex[:8]
        context = BTContext(blackboard=dict(self.places), on_event=self._on_node_event, on_record=self._on_record, run_id=run_id)

        try:
            tree = BehaviorTree(xml_text, make_leaf_factory(self.registry, self.runner), context)
        except (TreeError, ValueError) as error:
            return self._reject(f'tree does not build: {error}')

        allowed, note = preflight_gate(tree.id, self.preflight_passed, skip, self.require_preflight)

        if not allowed:
            return self._reject(note, tree_id=tree.id)

        self._start(tree, run_id, note, xml_text, blind)

        return True

    def cancel(self):
        if self.tree is None:
            return

        self.tree.halt()
        self.trace.emit('halted', f'{self.tree.id} cancelled', level='warn')
        self._clear_tree()
        self._status('cancelled')

    def halt(self):
        """Stop the running tree without a report, at shutdown"""
        if self.tree is not None:
            self.tree.halt()

    def _recorder_refusal(self) -> str:
        if not self.require_recorder:
            return ''

        ready, reason = self.recorder_ready()

        return '' if ready else f'tree refused: the recorder is not ready ({reason})'

    def _validation_refusal(self, xml_text: str, blind: bool) -> str:
        """Trace every issue but the maturity warnings; the refusal when there are errors"""
        config = ValidatorConfig(essential_devices=self.essential_devices, blind=blind, known_keys=set(self.places))
        issues = TreeValidator(self.registry, config).validate(xml_text)
        errors = [issue for issue in issues if issue.level == 'error']

        for issue in issues:
            if issue.code == 'maturity':
                continue                                # every skill is wip for now: not worth a trace line

            self.trace.emit('validation', f'{issue.path}: {issue.message}', level='error' if issue.level == 'error' else 'warn')

        return f'tree rejected: {len(errors)} errors' if errors else ''

    def _reject(self, text: str, **data) -> bool:
        self.trace.emit('rejected', text, level='error', **data)
        self._status('rejected')

        return False

    def _start(self, tree: BehaviorTree, run_id: str, note: str, xml_text: str, blind: bool):
        if self.tree is not None:
            self.tree.halt()
            self.trace.emit('halted', f'halted {self.tree.id} for a new tree', level='warn')
            self._clear_tree()

        if tree.id == PREFLIGHT_TREE:
            self.preflight_passed = False   # a preflight that does not succeed closes the gate again

        if note:
            self.trace.emit('preflight_skipped', note, level='warn', tree_id=tree.id)

        self.tree = tree

        self.trace.fields = {'run_id': run_id, 'tree_id': tree.id}
        self.trace.emit('started', f'running {tree.id}' + (' (blind)' if blind else ''), tree=xml_text, blind=blind)
        self._status('running')

    # ========== Ticks ==========

    def tick(self):
        if self.tree is None:
            return

        try:
            status = self.tree.tick()
        except Exception as error:                      # a skill or runner bug must not take the executor down
            self.tree.halt()
            self.trace.emit('error', f'{self.tree.id} stopped by an internal error: {error!r}', level='error')
            self.logger.error(f'tick failed: {error!r}')
            self._status('error')
            self._clear_tree()
            return

        if status != Status.RUNNING:
            self._finish(status)

    def _finish(self, status: Status):
        level  = 'info' if status == Status.SUCCESS else 'error'
        reason = self.tree.context.abort_reason
        text   = f'{self.tree.id}: {status.value}' + (f' (aborted by {reason})' if reason else '')
        self.trace.emit('finished', text, level=level, status=status.value, aborted=reason)

        if self.tree.id == PREFLIGHT_TREE:
            self.preflight_passed = status == Status.SUCCESS
        self._status(status.value)
        self._clear_tree()

    def _clear_tree(self):
        """The run is over: no tree, and no run_id on later events"""
        self.tree         = None
        self.trace.fields = {}

    def _status(self, state: str):
        self.on_status(state, self.tree.id if self.tree else None)

    # ========== Trace ==========

    def _on_node_event(self, node, event: dict):
        if event['status'] == Status.IDLE.value:
            return

        skill  = isinstance(node, SkillLeaf)
        failed = event['status'] == Status.FAILURE.value

        if not skill and not (failed and event['message']):
            return                                      # a control node matters only when it fails for its own reason

        text = f'{event["node"]} {event["status"]}'

        if skill and event['status'] == Status.RUNNING.value and node.inputs:
            text += ' ' + node.describe_inputs()

        if event['message']:
            text += f': {event["message"]}'

        self.trace.emit('node', text, level='warn' if failed else 'info', **event)

    def _on_record(self, record: dict):
        """A finished action call or an agent decision; a failure carries its feedback, for review after the run"""
        if 'decision' in record:
            text = f'{record["skill"]} decided {record["decision"]}: {record["reason"]}'
        else:
            text = f'{record["skill"]} {record["status"]} in {record["elapsed_s"]} s: {record["message"]}'

        level = 'warn' if record.get('status') == Status.FAILURE.value else 'info'
        self.trace.emit('record', text, level=level, record=record)
