"""The external planner backend: a session outside the planner writes the plan, e.g. Claude Code over ssh.

The planner publishes each model call as a request (latched JSON on topics.PLANNER_REQUEST_TOPIC: the call id, the system prompt,
and the conversation so far - the task, then the validator's feedback on each earlier answer), then waits. The
session answers with planner_cli on topics.PLANNER_ANSWER_TOPIC: a short explanation and a <tree>...</tree> or <use_tree id="..."/>.
The planner checks the answer as it checks a model's: a tree that fails the validator comes back as a new request.

This module needs no ROS: the planner node wires the publisher and passes answers in with deliver().
"""

import time
import queue
import uuid

from typing import Callable

from .llm_backends import BackendError, Reply




class ExternalPlannerBackend:
    """One reply per call, from a session"""

    name = 'external'

    def __init__(
        self,
        publish_request: Callable[[dict], None],
        timeout_s: float = 600.0,
    ):
        """publish_request sends a request dict; timeout_s is how long a call waits for the session."""
        self._publish_request = publish_request
        self._answers         = queue.Queue()
        self.timeout_s        = timeout_s

    def deliver(
        self,
        answer: dict,
    ) -> None:
        """An answer message from the session; called from the subscription."""
        self._answers.put(answer)

    def complete(
        self,
        system: str,
        messages: list[dict],
    ) -> Reply:
        """Publish the call, wait for the session's answer to it, and return it as the model's reply."""
        request = {
            'id':       uuid.uuid4().hex[:8],
            'state':    'open',
            'opened':   time.time(),
            'system':   system,
            'messages': list(messages),             # a copy: the planner appends to its list after this call
        }

        while not self._answers.empty():            # answers sent before this call opened are stale
            self._answers.get_nowait()

        self._publish_request(request)
        deadline = time.monotonic() + self.timeout_s

        try:
            while time.monotonic() < deadline:
                try:
                    answer = self._answers.get(timeout=0.2)
                except queue.Empty:
                    continue

                if answer.get('id') == request['id'] and str(answer.get('text', '')).strip():
                    text = answer['text']
                    return Reply(text, '', {'role': 'assistant', 'content': text})

            raise BackendError(f'no answer from the session to call {request["id"]} within {self.timeout_s:g} s')
        finally:
            self._publish_request({**request, 'state': 'closed', 'closed': time.time()})
