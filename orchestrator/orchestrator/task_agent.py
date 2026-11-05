"""The task agent's desk in the executor: a session outside it (Claude Code over ssh, with agent_cli) does a task
step by step (docs/task-mode.md).

A task sentence opens a task. The executor publishes it (latched JSON on OPEN_TOPIC: run_id, the sentence, blind,
the places, what the knowledge store knows, the scratch folder) and waits for commands on COMMAND_TOPIC:

- plan: the task plan, a list of subgoals; it goes in the trace (each version) and in plan.md in the scratch folder
- run: one skill as the next step, for a subgoal; the reply comes when the step ends, with its result
- done, give_up: end the task

Each command gets one reply on REPLY_TOPIC with its id. Limits: max_steps and max_task_s end the task as gave_up.
When no command comes for idle_s between steps (the session or its cloud link is gone), the robot says so once on
/speak and waits.

This module needs no ROS: the executor wires the topics, calls command() from a worker thread (a run blocks until
its step ends) and check() from its timer.
"""

import time
import threading

from typing import Callable
from pathlib import Path
from dataclasses import asdict

from .tasks import Task, Tasks
from .behavior_tree import Context


OPEN_TOPIC    = '/orchestrator/task/open'
COMMAND_TOPIC = '/orchestrator/task/command'
REPLY_TOPIC   = '/orchestrator/task/reply'
LOST_SPEECH   = 'I lost the connection to my planner. I will wait here. Please help me if you can.'


class TaskDesk:
    def __init__(self, tasks: Tasks, publish_open: Callable[[dict], None], publish_reply: Callable[[dict], None],
                 speak: Callable[[str], None], known: Callable[[], str] = lambda: '', max_steps: int = 40,
                 max_task_s: float = 900.0, idle_s: float = 120.0, clock: Callable[[], float] = time.monotonic):
        self.tasks         = tasks
        self.publish_open  = publish_open
        self.publish_reply = publish_reply
        self.speak         = speak
        self.known         = known                   # the knowledge store's prompt section, for the open task
        self.max_steps     = max_steps
        self.max_task_s    = max_task_s
        self.idle_s        = idle_s
        self.clock         = clock
        self.task          = None                    # the open task, and when it opened and last heard from the agent
        self.opened        = 0.0
        self.heard         = 0.0
        self.spoke_lost    = False
        self.plans         = 0
        self.busy          = 0                       # commands in progress: a session waiting for a step is not quiet
        self._lock         = threading.Lock()        # open and check on the spin thread, commands on worker threads

    def open(self, sentence: str, context: Context, blind: bool = False):
        task = self.tasks.start_task('task', context, blind)

        with self._lock:
            self.task = task

        self.opened     = self.heard = self.clock()
        self.spoke_lost = False
        self.plans      = 0
        self.publish_open({
            'state':   'open',
            'run_id':  context.run_id,
            'task':    sentence,
            'blind':   blind,
            'places':  sorted(context.blackboard),
            'known':   self.known(),
            'scratch': context.scratch_dir,
            'limits':  {'max_steps': self.max_steps, 'max_task_s': self.max_task_s},
        })

    def command(self, message: dict):
        """One command from the session; the reply goes out with the command's id"""
        reply = {'id': message.get('id'), 'op': message.get('op')}

        with self._lock:
            self.busy += 1

        try:
            reply |= self._command(message)
        except (KeyError, TypeError, ValueError) as error:
            reply |= {'ok': False, 'message': f'bad command: {error!r}'}
        except OSError as error:
            reply |= {'ok': False, 'message': f'could not write: {error!r}'}
        finally:
            with self._lock:
                self.busy -= 1
                self.heard = self.clock()

        self.publish_reply(reply)

    def check(self):
        """From the executor's timer: the time limit, a session that went quiet, and a task that ended elsewhere"""
        task = self.task

        if task is None:
            return

        if self.tasks.task is not task:                  # cancelled, or a tree sent alone replaced it
            self._close(task, 'ended')
        elif self.clock() - self.opened > self.max_task_s:
            self._end(task, 'gave_up', f'the task took longer than max_task_s {self.max_task_s:.0f} s')
        elif not self.busy and not self.spoke_lost and self.clock() - self.heard > self.idle_s:
            self.spoke_lost = True
            self.speak(LOST_SPEECH)
            self.tasks.tracer.emit('agent_lost', f'no command for {self.idle_s:.0f} s; said so and waiting', level='warn')

    # ========== Commands ==========

    def _command(self, message: dict) -> dict:
        task = self.task

        if task is None or message.get('run_id') != task.run_id:
            return {'ok': False, 'message': 'no open task with this run_id; run agent_cli task wait'}

        self.heard      = self.clock()
        self.spoke_lost = False
        op              = message['op']

        if op == 'plan':
            return self._plan(task, [str(subgoal) for subgoal in message['subgoals']])

        if op == 'run':
            return self._run(task, message['skill'], dict(message.get('inputs') or {}), str(message.get('subgoal', '')))

        if op in ('done', 'give_up'):
            return self._end(task, 'done' if op == 'done' else 'gave_up', str(message.get('reason', '')))

        return {'ok': False, 'message': f'unknown op {op}; use plan, run, done or give_up'}

    def _plan(self, task: Task, subgoals: list[str]) -> dict:
        if not subgoals:
            return {'ok': False, 'message': 'a plan needs at least one subgoal'}

        version = self.plans + 1
        text    = '\n'.join(f'{index}. {subgoal}' for index, subgoal in enumerate(subgoals, 1))

        if task.context.scratch_dir:
            Path(task.context.scratch_dir, 'plan.md').write_text(f'# Task plan, version {version}\n\n{text}\n')

        self.plans = version
        self.tasks.tracer.emit('plan', f'plan v{version}:\n{text}', subgoals=subgoals, version=version)

        return {'ok': True, 'message': f'plan v{self.plans} saved', 'version': self.plans}

    def _run(self, task: Task, skill: str, inputs: dict, subgoal: str) -> dict:
        if task.steps >= self.max_steps:
            return {'ok': False, 'message': f'max_steps {self.max_steps} reached: end the task with done or give_up'}

        result = self.tasks.run_skill(skill, inputs, subgoal=subgoal)

        return {'ok': result.status != 'error', **asdict(result)}

    def _end(self, task: Task, end: str, reason: str) -> dict:
        self.tasks.end_task(task, end, reason)
        self._close(task, end)

        return {'ok': True, 'message': f'the task ended: {end}'}

    def _close(self, task: Task, end: str):
        with self._lock:                                 # check() and a command can both close it; a new task may be open
            if self.task is not task:
                return

            self.task = None

        self.publish_open({'state': 'closed', 'run_id': task.run_id, 'end': end})
