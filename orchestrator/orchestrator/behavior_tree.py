"""A small BehaviorTree.CPP v4 interpreter: the same XML, run from Python"""

import time
import xml.etree.ElementTree as ElementTree

from enum import Enum
from typing import Callable, Protocol


class Status(Enum):
    IDLE    = 'idle'
    RUNNING = 'running'
    SUCCESS = 'success'
    FAILURE = 'failure'


DONE = (Status.SUCCESS, Status.FAILURE)


class TreeError(ValueError):
    pass


class Context:
    """What every node of one tree shares"""

    def __init__(
        self,
        blackboard: dict | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[['TreeNode', dict], None] | None = None
    ):
        self.blackboard = {} if blackboard is None else blackboard
        self.clock      = clock
        self.on_event   = on_event or (lambda node, event: None)


class TreeNode:
    def __init__(self, tag: str, path: str, attributes: dict, children: list['TreeNode'], context: Context):
        self.tag        = tag
        self.path       = path
        self.attributes = attributes
        self.children   = children
        self.context    = context
        self.status     = Status.IDLE
        self.message    = ''

    def tick(self) -> Status:
        status = self._tick()
        self._set_status(status)

        return status

    def halt(self):
        """Stop if running, and forget any result, so the next tick starts fresh"""
        for child in self.children:
            if child.status != Status.IDLE:
                child.halt()

        if self.status == Status.RUNNING:
            self._halt()

        self._set_status(Status.IDLE)

    def reset_children(self):
        for child in self.children:
            child.halt()

    def _tick(self) -> Status:
        raise NotImplementedError

    def _halt(self):
        pass

    def _set_status(self, status: Status):
        if status == self.status:
            return

        self.status = status
        self.context.on_event(self, {
            'time':    self.context.clock(),
            'path':    self.path,
            'node':    self.tag,
            'status':  status.value,
            'message': self.message,
        })

    def _int(self, name: str, default: int | None = None) -> int:
        return int(self._attribute(name, default))

    def _attribute(self, name: str, default=None):
        value = self.attributes.get(name, default)

        if value is None:
            raise TreeError(f'{self.path}: {self.tag} needs attribute {name}')

        return value


# ========== Control nodes ==========

class Sequence(TreeNode):
    """Children in order; a running child is resumed next tick.

    A Sequence stops at the first failure, a Fallback (stop_on SUCCESS) at the first success.
    Without memory, stopping starts the children over.
    """

    stop_on = Status.FAILURE
    memory  = False

    def __init__(self, *args):
        super().__init__(*args)

        self.index = 0

    def _tick(self) -> Status:
        while self.index < len(self.children):
            status = self.children[self.index].tick()

            if status == Status.RUNNING:
                return status

            if status == self.stop_on:
                if not self.memory:
                    self.index = 0
                    self.reset_children()

                return status

            self.index += 1

        self.index = 0
        self.reset_children()

        return Status.SUCCESS if self.stop_on == Status.FAILURE else Status.FAILURE

    def _halt(self):
        self.index = 0


class SequenceWithMemory(Sequence):
    memory = True


class Fallback(Sequence):
    """Children in order until one succeeds"""

    stop_on = Status.SUCCESS


class ReactiveSequence(TreeNode):
    """All children from the first, every tick; a later running child is halted when an earlier one stops it.

    ReactiveSequence stops on failure, ReactiveFallback (stop_on SUCCESS) on success.
    """

    stop_on = Status.FAILURE

    def _tick(self) -> Status:
        for index, child in enumerate(self.children):
            status = child.tick()

            if status != self.stop_on and status != Status.RUNNING:
                continue

            for later in range(index + 1, len(self.children)):
                self.children[later].halt()

            if status == self.stop_on:
                self.reset_children()

            return status

        self.reset_children()

        return Status.SUCCESS if self.stop_on == Status.FAILURE else Status.FAILURE


class ReactiveFallback(ReactiveSequence):
    stop_on = Status.SUCCESS


class Parallel(TreeNode):
    """All children every tick; success_count of them (default all) must succeed"""

    def __init__(self, *args):
        super().__init__(*args)

        success_count      = self._int('success_count', -1)
        self.success_count = len(self.children) if success_count < 0 else success_count
        self.failure_count = self._int('failure_count', 1)

    def _tick(self) -> Status:
        for child in self.children:
            if child.status not in DONE:
                child.tick()

        statuses    = [child.status for child in self.children]
        num_success = statuses.count(Status.SUCCESS)
        num_failure = statuses.count(Status.FAILURE)

        if num_success >= self.success_count:
            self.reset_children()
            return Status.SUCCESS

        if num_failure >= self.failure_count or num_failure > len(self.children) - self.success_count:
            self.reset_children()
            return Status.FAILURE

        return Status.RUNNING


class IfThenElse(TreeNode):
    """First child is the condition; second runs if it succeeds, third (if any) if it fails"""

    def __init__(self, *args):
        super().__init__(*args)

        if len(self.children) not in (2, 3):
            raise TreeError(f'{self.path}: IfThenElse needs 2 or 3 children')

        self.branch = None

    def _tick(self) -> Status:
        if self.branch is None:
            condition = self.children[0].tick()

            if condition == Status.RUNNING:
                return condition

            if condition == Status.SUCCESS:
                self.branch = self.children[1]
            elif len(self.children) == 3:
                self.branch = self.children[2]
            else:
                return Status.FAILURE

        status = self.branch.tick()

        if status in DONE:
            self.branch = None
            self.reset_children()

        return status

    def _halt(self):
        self.branch = None


# ========== Decorators ==========

class Decorator(TreeNode):
    def __init__(self, *args):
        super().__init__(*args)

        if len(self.children) != 1:
            raise TreeError(f'{self.path}: {self.tag} needs exactly one child')

    @property
    def child(self) -> TreeNode:
        return self.children[0]


INVERTED = {Status.SUCCESS: Status.FAILURE, Status.FAILURE: Status.SUCCESS}


class Inverter(Decorator):
    def _tick(self) -> Status:
        status = self.child.tick()

        return INVERTED.get(status, status)


class ForceSuccess(Decorator):
    """When the child is done, report `result` whatever the child returned"""

    result = Status.SUCCESS

    def _tick(self) -> Status:
        return Status.RUNNING if self.child.tick() == Status.RUNNING else self.result


class ForceFailure(ForceSuccess):
    result = Status.FAILURE


class RetryUntilSuccessful(Decorator):
    """Run the child again after each failure, up to num_attempts (-1: no limit)"""

    def __init__(self, *args):
        super().__init__(*args)

        self.num_attempts = self._int('num_attempts')
        self.attempts     = 0

    def _tick(self) -> Status:
        status = self.child.tick()

        if status != Status.FAILURE:
            if status == Status.SUCCESS:
                self.attempts = 0

            return status

        self.attempts += 1
        self.child.halt()

        if 0 <= self.num_attempts <= self.attempts:
            self.attempts = 0
            return Status.FAILURE

        return Status.RUNNING       # next attempt on the next tick

    def _halt(self):
        self.attempts = 0


class Repeat(Decorator):
    def __init__(self, *args):
        super().__init__(*args)

        self.num_cycles = self._int('num_cycles')
        self.cycles     = 0

    def _tick(self) -> Status:
        status = self.child.tick()

        if status != Status.SUCCESS:
            if status == Status.FAILURE:
                self.cycles = 0

            return status

        self.cycles += 1
        self.child.halt()

        if 0 <= self.num_cycles <= self.cycles:
            self.cycles = 0
            return Status.SUCCESS

        return Status.RUNNING

    def _halt(self):
        self.cycles = 0


class KeepRunningUntilFailure(Decorator):
    def _tick(self) -> Status:
        status = self.child.tick()

        if status == Status.SUCCESS:
            self.child.halt()
            return Status.RUNNING

        return status


class Timed:
    """A start time set on the first tick and cleared on halt or when done"""

    start_time = None

    def elapsed(self) -> float:
        if self.start_time is None:
            self.start_time = self.context.clock()

        return self.context.clock() - self.start_time

    def _halt(self):
        self.start_time = None


class Timeout(Timed, Decorator):
    def __init__(self, *args):
        super().__init__(*args)

        self.limit_s = self._int('msec') / 1000

    def _tick(self) -> Status:
        if self.elapsed() > self.limit_s:
            self.child.halt()
            self.start_time = None
            self.message    = 'timeout'

            return Status.FAILURE

        status = self.child.tick()

        if status in DONE:
            self.start_time = None

        return status


class Delay(Timed, Decorator):
    def __init__(self, *args):
        super().__init__(*args)

        self.delay_s = self._int('delay_msec') / 1000

    def _tick(self) -> Status:
        if self.elapsed() < self.delay_s:
            return Status.RUNNING

        status = self.child.tick()

        if status in DONE:
            self.start_time = None

        return status


class RunOnce(Decorator):
    def __init__(self, *args):
        super().__init__(*args)

        self.result = None

    def _tick(self) -> Status:
        if self.result is not None:
            return self.result

        status = self.child.tick()

        if status in DONE:
            self.result = status

        return status


# ========== Built-in leaves ==========

class AlwaysSuccess(TreeNode):
    def _tick(self) -> Status:
        return Status.SUCCESS


class AlwaysFailure(TreeNode):
    def _tick(self) -> Status:
        return Status.FAILURE


class Sleep(Timed, TreeNode):
    def __init__(self, *args):
        super().__init__(*args)

        self.duration_s = self._int('msec') / 1000

    def _tick(self) -> Status:
        if self.elapsed() < self.duration_s:
            return Status.RUNNING

        self.start_time = None

        return Status.SUCCESS


class SetBlackboard(TreeNode):
    def _tick(self) -> Status:
        key = self._attribute('output_key').strip('{}')
        self.context.blackboard[key] = self._attribute('value')

        return Status.SUCCESS


NODE_TYPES = {
    node_type.__name__: node_type
    for node_type in [
        Sequence, SequenceWithMemory, ReactiveSequence, Fallback, ReactiveFallback, Parallel, IfThenElse,
        Inverter, ForceSuccess, ForceFailure, RetryUntilSuccessful, Repeat, KeepRunningUntilFailure,
        Timeout, Delay, RunOnce,
        AlwaysSuccess, AlwaysFailure, Sleep, SetBlackboard,
    ]
}


# ========== Building a tree from XML ==========

class LeafFactory(Protocol):
    def __call__(self, skill_name: str, path: str, attributes: dict, context: Context) -> TreeNode: ...


class BehaviorTree:
    """One tree from a BehaviorTree.CPP v4 XML document, ready to tick"""

    def __init__(self, xml_text: str, leaf_factory: LeafFactory, context: Context | None = None, main_tree: str | None = None):
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError as error:
            raise TreeError(f'XML does not parse: {error}') from error

        self.context = context or Context()
        self.trees   = {tree.get('ID'): tree for tree in root.findall('BehaviorTree')}
        self._leaves = leaf_factory

        if not self.trees:
            raise TreeError('no <BehaviorTree> found')

        main_tree = main_tree or root.get('main_tree_to_execute') or next(iter(self.trees))

        self.id   = main_tree
        self.root = self._build_tree(main_tree, main_tree, ())

    def tick(self) -> Status:
        return self.root.tick()

    def halt(self):
        self.root.halt()

    def nodes(self) -> list[TreeNode]:
        found, stack = [], [self.root]

        while stack:
            node = stack.pop()
            found.append(node)
            stack.extend(reversed(node.children))

        return found

    def _build_tree(self, tree_id: str, path: str, visiting: tuple[str, ...]) -> TreeNode:
        if tree_id not in self.trees:
            raise TreeError(f'unknown SubTree {tree_id}')

        if tree_id in visiting:
            raise TreeError(f'SubTree {tree_id} includes itself')

        children = list(self.trees[tree_id])

        if len(children) != 1:
            raise TreeError(f'{tree_id}: a BehaviorTree needs exactly one root node')

        return self._build(children[0], f'{path}/{children[0].tag}', (*visiting, tree_id))

    def _build(self, element: ElementTree.Element, path: str, visiting: tuple[str, ...]) -> TreeNode:
        tag        = element.tag
        attributes = dict(element.attrib)

        if tag == 'SubTree':
            return self._build_tree(attributes.get('ID', ''), path, visiting)

        if tag in NODE_TYPES:
            children = [
                self._build(child, f'{path}[{index}]/{child.tag}', visiting)
                for index, child in enumerate(element)
            ]

            return NODE_TYPES[tag](tag, path, attributes, children, self.context)

        if len(element):
            raise TreeError(f'{path}: skill {tag} cannot have children')

        skill_name = attributes.pop('ID') if tag in ('Action', 'Condition') else tag

        return self._leaves(skill_name, path, attributes, self.context)
