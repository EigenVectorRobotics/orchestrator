"""Runs one tree at a time: builds it, starts it in place of the running one, ticks, cancels and traces it."""

import uuid

from typing import Callable, Protocol

from .leaf import SkillLeaf
from .engine import BehaviorTree, BTContext, LeafFactory, Status


class Trace(Protocol):
    fields: dict

    def emit(self, kind: str, text: str, level: str = 'info', **data): ...


class Logger(Protocol):
    def error(self, message: str): ...


class TreeRunner:
    """Runs one tree at a time: builds it, starts it in place of the running one, ticks, cancels and halts it, and
    traces each step. Which trees may run is the caller's decision"""

    def __init__(self, leaf_factory: LeafFactory, trace: Trace, on_status: Callable[[str, str | None], None],
                 logger: Logger, blackboard: dict | None = None,
                 on_finished: Callable[[str, Status], None] = lambda tree_id, status: None):
        self.leaf_factory = leaf_factory
        self.trace        = trace
        self.on_status    = on_status       # (state, ID of the tree running or None)
        self.logger       = logger
        self.blackboard   = blackboard or {}   # copied into every tree, e.g. the places
        self.on_finished  = on_finished     # a tree ended by itself with success or failure
        self.tree: BehaviorTree | None = None

    def build(self, xml_text: str) -> BehaviorTree:
        """A tree ready to start, with its own run_id; raises TreeError or ValueError when it does not build"""
        context = BTContext(blackboard=dict(self.blackboard), on_event=self._on_node_event, on_record=self._on_record,
                            run_id=uuid.uuid4().hex[:8])

        return BehaviorTree(xml_text, self.leaf_factory, context)

    def start(self, tree: BehaviorTree, detail: str = '', /, **data):
        """Halt the running tree, if any, and run tree; detail and data go on the started event"""
        if self.tree is not None:
            self.tree.halt()
            self.trace.emit('halted', f'halted {self.tree.id} for a new tree', level='warn')
            self._clear_tree()

        self.tree = tree

        self.trace.fields = {'run_id': tree.context.run_id, 'tree_id': tree.id}
        self.trace.emit('started', f'running {tree.id}{detail}', **data)
        self._status('running')

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

        self.on_finished(self.tree.id, status)
        self._status(status.value)
        self._clear_tree()

    def _clear_tree(self):
        """The run is over: no tree, and no run_id on later events"""
        self.tree         = None
        self.trace.fields = {}

    def _status(self, state: str):
        self.on_status(state, self.tree.id if self.tree else None)

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
