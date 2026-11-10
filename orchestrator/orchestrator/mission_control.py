"""Mission Control's adapter, without ROS: the state the page shows, and what a click does.

mission_control_node.py wires the ROS topics, the web server and the files. This module holds the state of a run
and the rules of a command, so that both can be read and tested alone.

The page reads one state message (GET /api/state) and sends a task, a step decision or a cancel (POST). A
decision goes out on the same topic as `agent_cli step`, so the gate, the limits and the recorder see no
difference. The step gate itself is the only judge of a decision (StepGate.decide in task_agent.py): this module
checks the HTTP body, sends the decision and relays the gate's answer.
"""

import hmac
import time
import threading

from collections import deque
from typing import Callable

from .images import ImageError, encode_rgb, image_to_rgb


MAX_TASK_CHARS = 400                    # a task sentence; longer is a plan, not a task
TRACE_KEEP     = 400                    # trace events the page can scroll back through
DECISION_WAIT  = 5.0                    # seconds to wait for the executor's reply to a decision
TASK_FIELDS    = ('state', 'run_id', 'task', 'blind', 'end', 'limits')   # of the open message; not places, known or scratch
CAMERA_SIDE    = 960                    # the long side of the frame the page gets, in pixels
CAMERA_QUALITY = 80


class MissionState:
    """The latest message of each topic the page shows, and the last trace events with a cursor.

    The ROS callbacks write it on the spin thread; the web server reads it on its own threads.
    """

    def __init__(self, keep: int = TRACE_KEEP):
        self._lock   = threading.Lock()
        self._parts  = {}                            # status, task, step, recorder: the latest message of each
        self._plan   = {}                            # the newest plan of the open task, from the trace
        self._events = deque(maxlen=keep)
        self._seq    = 0

    def put(self, part: str, message: dict):
        """The latest message of one part; a new task forgets the plan of the last one"""
        with self._lock:
            if part == 'task' and message.get('run_id') != self._parts.get('task', {}).get('run_id'):
                self._plan = {}

            self._parts[part] = message

    def trace(self, event: dict):
        """One /orchestrator/trace event; a plan event is also kept as the plan"""
        with self._lock:
            self._seq   += 1
            event['seq'] = self._seq
            self._events.append(event)

            if event.get('kind') == 'plan' and event.get('subgoals'):
                self._plan = {'version': event.get('version'), 'subgoals': list(event['subgoals'])}

    def snapshot(self, after: int = 0) -> dict:
        """Everything the page shows; only the trace events newer than after. The task's open message is cut down
        to TASK_FIELDS, because its places and what the knowledge store knows would be resent at every poll"""
        with self._lock:
            task = self._parts.get('task', {})

            return {
                'time':   time.time(),
                'seq':    self._seq,
                'keep':   self._events.maxlen,
                'plan':   self._plan,
                'trace':  [] if after >= self._seq else [event for event in self._events if event['seq'] > after],
                'task':   {field: task[field] for field in TASK_FIELDS if field in task},
                **{part: self._parts.get(part, {}) for part in ('status', 'step', 'recorder')},
            }


def token_ok(expected: str, given: str) -> bool:
    """True when a command carries the node's token. `off` takes the check away, for a bench with no agent on it"""
    return expected == 'off' or (bool(expected) and hmac.compare_digest(expected, given or ''))


class Camera:
    """The latest camera frame, encoded only when the page asks for one: the robot publishes faster than a person
    looks. No ROS here either: images.py reads any message with height, width, step, encoding and data"""

    def __init__(self, topic: str = '', max_side: int = CAMERA_SIDE, quality: int = CAMERA_QUALITY):
        self.topic     = topic
        self.max_side  = max_side
        self.quality   = quality
        self._lock     = threading.Lock()
        self._message  = None
        self._at       = 0.0
        self._encoded  = None                        # (the message it came from, bytes, media type)

    def put(self, message):
        with self._lock:
            self._message, self._at, self._encoded = message, time.time(), None

    def state(self) -> dict:
        """What the page shows next to the frame"""
        with self._lock:
            message = self._message

            if message is None:
                return {'have': False, 'topic': self.topic}

            return {'have': True, 'topic': self.topic, 'stamp': self._at, 'age_s': round(time.time() - self._at, 1),
                    'width': message.width, 'height': message.height}

    def frame(self) -> tuple[bytes, str] | None:
        """The latest frame as JPEG, or None; the encoding runs on the web server's thread, not on the spin thread"""
        with self._lock:
            message, encoded = self._message, self._encoded

        if message is None:
            return None

        if encoded is not None and encoded[0] is message:
            return encoded[1], encoded[2]

        try:
            body, media_type = encode_rgb(image_to_rgb(message), max_side=self.max_side, quality=self.quality)
        except ImageError:
            return None

        with self._lock:
            if self._message is message:
                self._encoded = (message, body, media_type)

        return body, media_type


class Commands:
    """What a click does. Every command answers (code, payload)."""

    def __init__(self, send_task: Callable[[dict], bool], decide_step: Callable[[dict], dict | None],
                 send_cancel: Callable[[], bool]):
        self.send_task   = send_task
        self.decide_step = decide_step               # publishes the decision and waits for the executor's reply
        self.send_cancel = send_cancel

    def task(self, body: dict) -> tuple[int, dict]:
        sentence = ' '.join(str(body.get('task') or '').split())

        if not sentence:
            return 400, {'ok': False, 'message': 'a task needs a sentence'}

        if len(sentence) > MAX_TASK_CHARS:
            return 400, {'ok': False, 'message': f'a task sentence is {MAX_TASK_CHARS} characters at most, not {len(sentence)}'}

        blind = bool(body.get('blind'))

        if not self.send_task({'task': sentence, 'blind': blind}):
            return 504, {'ok': False, 'message': 'the executor does not listen for a task; is it running?'}

        return 200, {'ok': True, 'message': 'task sent' + (' (blind)' if blind else '')}

    def step(self, body: dict) -> tuple[int, dict]:
        """Approve or refuse a step. The gate takes or refuses the decision; this only relays its answer"""
        approve = body.get('approve')
        step_id = str(body.get('id') or '')

        if not isinstance(approve, bool):
            return 400, {'ok': False, 'message': 'a decision needs approve: true or approve: false'}

        if not step_id:
            return 400, {'ok': False, 'message': 'a decision names the step it is for'}

        reply = self.decide_step({'id': step_id, 'approve': approve, 'reason': str(body.get('reason') or ''),
                                  'by': 'mission_control'})

        if reply is None:
            return 504, {'ok': False, 'message': 'the executor did not take the decision; is it running?'}

        accepted = bool(reply.get('accepted'))

        return (200 if accepted else 409), {'ok': accepted, 'message': reply.get('message', '')}

    def cancel(self, body: dict) -> tuple[int, dict]:
        if not self.send_cancel():
            return 504, {'ok': False, 'message': 'the executor does not listen for a cancel; is it running?'}

        return 200, {'ok': True, 'message': 'cancel sent'}


class MissionApi:
    """The paths under /api; the node's web server does the HTTP and the files"""

    def __init__(self, state: MissionState, commands: Commands, camera: Camera | None = None):
        self.state  = state
        self.camera = camera or Camera()
        self.posts  = {'/api/task': commands.task, '/api/step': commands.step, '/api/cancel': commands.cancel}

    def get(self, path: str, query: dict) -> tuple[int, dict]:
        if path != '/api/state':
            return 404, {'ok': False, 'message': f'no such path {path}'}

        try:
            after = int(query.get('after', 0))
        except (TypeError, ValueError):
            after = 0

        return 200, {**self.state.snapshot(after), 'camera': self.camera.state()}

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        command = self.posts.get(path)

        if command is None:
            return 404, {'ok': False, 'message': f'no such path {path}'}

        if not isinstance(body, dict):
            return 400, {'ok': False, 'message': 'the body is a JSON object'}

        return command(body)
