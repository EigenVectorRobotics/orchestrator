"""The external agent backend: a session outside the executor answers agent calls, e.g. Claude Code over ssh.

The executor publishes each call as a request (latched JSON on REQUEST_TOPIC: the call id, phase, task, allowed
decisions, the failure with its feedback, and the history), then waits. The session gathers evidence with agent_cli
and sends one decision on DECISION_TOPIC. The executor checks it like any decide call and answers on REPLY_TOPIC;
a rejected decision leaves the call open. The call closes on an accepted decision, a cancel, or the leaf's timeout.

This module needs no ROS: the executor wires the publishers and passes decisions in with deliver().
"""

import json
import time
import queue
import uuid

from typing import Callable

from .agent import AgentError, ToolOutput


REQUEST_TOPIC   = '/orchestrator/agent/request'
DECISION_TOPIC  = '/orchestrator/agent/decision'
REPLY_TOPIC     = '/orchestrator/agent/reply'
DECISION_FIELDS = ('decision', 'reason', 'dx', 'dy', 'dyaw', 'use_left_arm')


class AgentStopped(AgentError):
    pass


def _evidence(call_tool: Callable[[str, dict], ToolOutput], name: str):
    output = call_tool(name, {})

    try:
        return json.loads(output.text)
    except json.JSONDecodeError:
        return None                                 # e.g. "no action has failed in this tree"


class ExternalAgentBackend:
    name = 'external'

    def __init__(self, publish_request: Callable[[dict], None], publish_reply: Callable[[dict], None]):
        self._publish_request = publish_request
        self._publish_reply   = publish_reply
        self._decisions       = queue.Queue()

    def deliver(self, decision: dict):
        """A decision message from the session; called from the subscription"""
        self._decisions.put(decision)

    def run(self, system: str, task: str, tools: list[dict], call_tool: Callable[[str, dict], ToolOutput],
            max_turns: int, should_stop: Callable[[], bool], on_event: Callable[..., None], skill: str = ''):
        decide  = next(tool for tool in tools if tool['name'] == 'decide')
        request = {
            'id':        uuid.uuid4().hex[:8],
            'state':     'open',
            'skill':     skill,
            'opened':    time.time(),
            'task':      task,
            'decisions': decide['input_schema']['properties']['decision']['enum'],
            'failure':   _evidence(call_tool, 'get_failure'),
            'history':   _evidence(call_tool, 'get_history'),
        }

        if any(tool['name'] == 'get_press' for tool in tools):
            request['press'] = _evidence(call_tool, 'get_press')

        while not self._decisions.empty():         # decisions sent before this call opened are stale
            self._decisions.get_nowait()

        self._publish_request(request)
        on_event('agent_wait', f'waiting for the session to decide call {request["id"]} on {DECISION_TOPIC}', call=request['id'])

        try:
            while True:
                if should_stop():
                    raise AgentStopped('cancelled or timed out')

                try:
                    message = self._decisions.get(timeout=0.2)
                except queue.Empty:
                    continue

                if message.get('id') != request['id']:
                    self._publish_reply({'id': message.get('id'), 'accepted': False, 'message': f'no open call {message.get("id")}; the open call is {request["id"]}'})
                    continue

                output = call_tool('decide', {key: message[key] for key in DECISION_FIELDS if key in message})
                self._publish_reply({'id': request['id'], 'accepted': output.final, 'message': output.text})

                if output.final:
                    return
        finally:
            self._publish_request({**request, 'state': 'closed', 'closed': time.time()})
