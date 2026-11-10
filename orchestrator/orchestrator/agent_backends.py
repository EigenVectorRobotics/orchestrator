"""Tool-use loops for agent skills that run inside the executor: any OpenAI-compatible endpoint (e.g. the Thor's
local model) and a script for tests. A session outside the executor (Claude Code) uses agent_external instead.

Each backend runs until decide succeeds, the turns run out, or the leaf cancels.
"""

import os
import json
import base64

from typing import Callable

from .agent import AgentError, ToolOutput
from .agent_external import AgentStopped
from .chat_completions import ChatCompletionsError, post_chat_completions


NUDGE = 'You have not decided yet. Gather what you still need, then call decide.'


class OpenAICompatibleAgentBackend:
    """Any OpenAI-compatible chat endpoint with tool calls: vLLM, llama-server, the Thor's local model"""

    name = 'openai_compatible'

    def __init__(self, base_url: str, model: str, api_key_env: str = 'OPENAI_API_KEY', max_tokens: int = 8000, timeout_s: float = 120.0):
        self.base_url   = base_url
        self.model      = model
        self.api_key    = os.environ.get(api_key_env, '')
        self.max_tokens = max_tokens
        self.timeout_s  = timeout_s

    def run(self, system: str, task: str, tools: list[dict], call_tool: Callable[[str, dict], ToolOutput],
            max_turns: int, should_stop: Callable[[], bool], on_event: Callable[..., None], skill: str = '',
            document: str = ''):
        functions = [
            {'type': 'function', 'function': {'name': tool['name'], 'description': tool['description'], 'parameters': tool['input_schema']}}
            for tool in tools
        ]
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': task}]

        for _ in range(max_turns):
            if should_stop():
                raise AgentStopped('cancelled')

            body = {'model': self.model, 'max_tokens': self.max_tokens, 'messages': messages, 'tools': functions, 'tool_choice': 'auto'}

            try:
                message = post_chat_completions(self.base_url, body, self.api_key, self.timeout_s)['choices'][0]['message']
            except (ChatCompletionsError, KeyError, IndexError) as error:
                raise AgentError(str(error)) from error

            calls     = message.get('tool_calls') or []
            assistant = {'role': 'assistant', 'content': message.get('content') or ''}

            if calls:
                assistant['tool_calls'] = calls

            messages.append(assistant)

            if message.get('reasoning_content'):
                on_event('agent_reasoning', message['reasoning_content'])

            if message.get('content'):
                on_event('agent_text', message['content'])

            if not calls:
                messages.append({'role': 'user', 'content': NUDGE})
                continue

            images, final = [], False

            for call in calls:
                try:
                    arguments = json.loads(call['function'].get('arguments') or '{}')
                    output    = call_tool(call['function']['name'], arguments)
                except json.JSONDecodeError as error:
                    output = ToolOutput(f'the arguments are not valid JSON: {error}', is_error=True)

                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': output.text or '(empty)'})
                final = final or output.final

                if output.image:
                    images.append({'type': 'image_url', 'image_url': {'url': f'data:{output.media_type};base64,{base64.b64encode(output.image).decode()}'}})

            if final:
                return

            if images:                              # tool messages carry text only; the images follow as a user turn
                messages.append({'role': 'user', 'content': [{'type': 'text', 'text': 'The images from get_image, in order.'}, *images]})

        raise AgentError(f'no decision after {max_turns} turns')


class ScriptedAgentBackend:
    """Plays tool calls from a script, one list of [tool, arguments] per agent call. For tests and dry runs"""

    name = 'scripted'

    def __init__(self, script: list[list]):
        self.script = [list(calls) for calls in script]
        self.calls  = []

    def run(self, system: str, task: str, tools: list[dict], call_tool: Callable[[str, dict], ToolOutput],
            max_turns: int, should_stop: Callable[[], bool], on_event: Callable[..., None], skill: str = '',
            document: str = ''):
        if not self.script:
            raise AgentError('no scripted agent call left')

        calls = self.script.pop(0) if len(self.script) > 1 else self.script[0]

        for name, arguments in calls:
            if should_stop():
                raise AgentStopped('cancelled')

            output = call_tool(name, dict(arguments))
            self.calls.append((name, arguments, output))

            if output.final:
                return

        raise AgentError('the script ended without a decision')
