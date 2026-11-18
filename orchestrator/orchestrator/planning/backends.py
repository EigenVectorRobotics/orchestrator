"""LLM backends for the planner: one reply per call, the conversation kept by the caller"""

import os

from typing import Protocol
from dataclasses import dataclass, field

from ..vlm.client import ChatCompletionsError, post_chat_completions


class BackendError(RuntimeError):
    pass


@dataclass
class Reply:
    text:      str                       # the visible answer
    reasoning: str = ''                  # a summary of the model's thinking, for the trace
    assistant: dict = field(default_factory=dict)   # the message to append to the conversation, as the backend wants it
    usage:     dict = field(default_factory=dict)


class Backend(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict]) -> Reply: ...


class ClaudeBackend:
    """Claude through the official SDK. Credentials come from ANTHROPIC_API_KEY or an `ant auth login` profile"""

    name = 'claude'

    def __init__(self, model: str = 'claude-opus-5-5', effort: str = 'high', max_tokens: int = 16000):
        import anthropic                    # here, so the other backends need no SDK

        self.anthropic  = anthropic
        self.client     = anthropic.Anthropic()
        self.model      = model
        self.effort     = effort
        self.max_tokens = max_tokens

    def complete(self, system: str, messages: list[dict]) -> Reply:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],   # the skill catalog: same every call
                messages=messages,
                thinking={'type': 'adaptive', 'display': 'summarized'},
                output_config={'effort': self.effort},
                betas=['server-side-fallback-2026-07-01'],
                fallbacks='default'
            )
        except self.anthropic.RateLimitError as error:
            raise BackendError(f'rate limited: {error.message}') from error
        except self.anthropic.APIStatusError as error:
            raise BackendError(f'API error {error.status_code}: {error.message}') from error
        except self.anthropic.APIConnectionError as error:
            raise BackendError(f'no connection to the API: {error}') from error

        if response.stop_reason == 'refusal':
            raise BackendError(f'the model declined: {response.stop_details}')

        text      = ''.join(block.text for block in response.content if block.type == 'text')
        reasoning = '\n'.join(block.thinking for block in response.content if block.type == 'thinking' and block.thinking)
        usage     = {
            'input_tokens':      response.usage.input_tokens,
            'output_tokens':     response.usage.output_tokens,
            'cache_read_tokens': response.usage.cache_read_input_tokens,
        }

        if response.stop_reason == 'max_tokens':
            raise BackendError('the reply hit max_tokens before the tree was complete')

        return Reply(text, reasoning, {'role': 'assistant', 'content': response.content}, usage)


class OpenAICompatibleBackend:
    """Any OpenAI-compatible chat endpoint: OpenAI, Gemini's OpenAI endpoint, vLLM, llama-server"""

    name = 'openai_compatible'

    def __init__(self, base_url: str, model: str, api_key_env: str = 'OPENAI_API_KEY', max_tokens: int = 16000, timeout_s: float = 300.0):
        self.base_url   = base_url.rstrip('/')
        self.model      = model
        self.api_key    = os.environ.get(api_key_env, '')
        self.max_tokens = max_tokens
        self.timeout_s  = timeout_s

    def complete(self, system: str, messages: list[dict]) -> Reply:
        body = {
            'model':      self.model,
            'max_tokens': self.max_tokens,
            'messages':   [{'role': 'system', 'content': system}, *messages],
        }
        try:
            payload = post_chat_completions(self.base_url, body, self.api_key, self.timeout_s)
        except ChatCompletionsError as error:
            raise BackendError(str(error)) from error

        message = payload['choices'][0]['message']
        text    = message.get('content') or ''

        return Reply(text, message.get('reasoning_content', ''), {'role': 'assistant', 'content': text}, payload.get('usage', {}))


class ScriptedBackend:
    """Replies from a list, for tests and for a dry run with no API key"""

    name = 'scripted'

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls   = []

    def complete(self, system: str, messages: list[dict]) -> Reply:
        self.calls.append(messages[-1]['content'])

        if not self.replies:
            raise BackendError('no scripted reply left')

        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

        return Reply(text, 'scripted reasoning', {'role': 'assistant', 'content': text})
