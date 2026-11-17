"""Fakes and builders that several test modules share.

Keep this module free of ROS imports: test_tasks, test_task_agent and test_task_loop import it, and they must
collect with no rclpy on the path"""

import time

from contextlib import contextmanager
from types import SimpleNamespace

from skill_registry import Registry

from orchestrator.tasks import Tasks
from orchestrator.task_agent import TaskDesk
from orchestrator.skill_leaf import make_leaf_factory
from orchestrator.behavior_tree import Context, Status

from data_paths import CONTRACTS


PANEL = {'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'frame': 'map'}


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeHandle:
    def __init__(self, script: list[Status], outputs: dict):
        self.script    = list(script)
        self.outputs   = outputs
        self.message   = ''
        self.cancelled = False

    def poll(self) -> Status:
        if self.cancelled:
            return Status.FAILURE

        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def cancel(self):
        self.cancelled = True


class FakeRunner:
    """Each skill plays a script of statuses, one per tick; default: success at once"""

    def __init__(self, scripts: dict[str, list] | None = None, outputs: dict | None = None):
        self.scripts = scripts or {}
        self.outputs = outputs or {}
        self.calls   = []
        self.handles = []

    def start(self, contract, inputs):
        self.calls.append((contract.name, inputs))

        script = self.scripts.get(contract.name, [Status.SUCCESS])

        if script and isinstance(script[0], list):     # one script per call
            script = script.pop(0) if len(script) > 1 else script[0]

        handle = FakeHandle(script, self.outputs.get(contract.name, {}))
        self.handles.append(handle)

        return handle


class FakeTracer:
    """Tracer's tap and fan-out, copied and not inherited, because the real Tracer needs rclpy"""

    def __init__(self):
        self.fields    = {}
        self.events    = []
        self.listeners = []

    @contextmanager
    def tap(self, listener):
        self.listeners.append(listener)

        try:
            yield
        finally:
            self.listeners.remove(listener)

    def emit(self, kind, text, level='info', **data):
        event = {'kind': kind, 'text': text, 'level': level, **self.fields, **data}

        self.events.append(event)

        for listener in tuple(self.listeners):
            listener(event)

    def kinds(self):
        return [event['kind'] for event in self.events if event['kind'] != 'validation']


def _tasks(runner: FakeRunner):
    registry = Registry.from_directory(CONTRACTS)
    tracer   = FakeTracer()
    statuses = []
    tasks    = Tasks(registry, make_leaf_factory(registry, runner), tracer, lambda state, tree: statuses.append(state),
                     {'nuc', 'orin'}, make_scratch=lambda run_id: f'/scratch/{run_id}')

    return tasks, tracer, statuses


def _context(run_id='r1'):
    return Context(blackboard={'call_panel': dict(PANEL)}, clock=FakeClock(), run_id=run_id)


def _tick_until(tasks, thread, max_ticks=200):
    for _ in range(max_ticks):
        tasks.tick()

        if not thread.is_alive():
            return

        time.sleep(0.005)


def _desk(runner: FakeRunner, tmp_path, **limits):
    """A desk with an open task. **limits goes to TaskDesk as it is, so a test can pass any of its arguments
    (max_steps, runbook, writer, stale, ran, ...); do not replace it with a fixed list"""
    tasks, tracer, _ = _tasks(runner)
    opened, replies  = [], []
    said, clock      = [], FakeClock()
    desk             = TaskDesk(tasks, opened.append, replies.append, said.append, clock=clock, **limits)
    context          = _context()
    context.blackboard['call_panel'] = {'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'frame': 'map', 'kind': 'place', 'floor': 'floor 2'}

    tasks.make_scratch = lambda run_id: str(tmp_path)
    desk.open('Go to the call panel', context)

    return SimpleNamespace(desk=desk, tasks=tasks, tracer=tracer, opened=opened, replies=replies, said=said, clock=clock)
