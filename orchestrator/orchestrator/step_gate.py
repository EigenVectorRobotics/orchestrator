"""The step gate: with it on (the real robot, at first), each run of the task desk waits for a person.

The request goes out latched on STEP_REQUEST_TOPIC, and a decision comes back on STEP_DECISION_TOPIC (reply on
STEP_REPLY_TOPIC). The gate takes the decision from any client on that topic, and keeps the client's name (`by`) in
the trace. Two clients exist: agent_cli step approve|refuse, which answers only at a terminal (a tty), so the
session that asked cannot answer itself by mistake; and Mission Control, where a person clicks in a browser. A
refused step does not run.

This module needs no ROS: the executor wires the topics.
"""

import time
import uuid
import threading

from typing import Callable


class StepGate:
    """A person approves or refuses each step before it runs"""

    def __init__(self, publish: Callable[[dict], None]):
        self.publish  = publish
        self.request  = None                    # the open request
        self.decision = None
        self.answered = threading.Event()
        self._lock    = threading.Lock()

    def ask(self, request: dict, still_open: Callable[[], bool]) -> dict:
        """Block until a person decides, or the task ends (cancel); returns {approve, state, reason}"""
        with self._lock:
            if self.request is not None:
                return {'approve': False, 'state': 'refused', 'reason': 'another step already waits for approval',
                        'by': 'the executor'}

            self.request  = {'id': uuid.uuid4().hex[:8], 'state': 'open', 'opened': time.time(), **request}
            self.decision = None
            self.answered.clear()

        self.publish(self.request)

        if not still_open():                    # it closed before the request was out: no cancel will come
            self.cancel('the task ended')

        self.answered.wait()

        with self._lock:
            request, decision, self.request = self.request, self.decision, None

        self.publish({**request, 'state': decision['state'], 'reason': decision['reason'], 'by': decision['by']})

        return decision

    def decide(self, message: dict) -> dict:
        """A decision for the open request; the reply says whether it was taken"""
        with self._lock:
            if self.request is not None and message.get('id') == self.request['id'] and self.answered.is_set():
                return {'id': message.get('id'), 'accepted': False, 'message': 'this step was decided already'}

            if self.request is None or message.get('id') != self.request['id']:
                return {'id': message.get('id'), 'accepted': False, 'message': 'no step with this id waits'}

            approve       = bool(message.get('approve'))
            self.decision = {'approve': approve, 'state': 'approved' if approve else 'refused',
                             'reason': str(message.get('reason', '')), 'by': str(message.get('by') or 'unknown')}
            self.answered.set()

        return {'id': message['id'], 'accepted': True, 'message': self.decision['state']}

    def cancel(self, reason: str):
        """Refuse the open request, if any: its task ended"""
        with self._lock:
            request = self.request

        if request is not None:
            self.decide({'id': request['id'], 'approve': False, 'reason': reason, 'by': 'the executor'})
