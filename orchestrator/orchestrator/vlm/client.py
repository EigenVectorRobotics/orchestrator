"""Closed questions about one image, over an OpenAI-compatible /chat/completions endpoint (the planner's
openai_compatible backend posts through post_chat_completions too)"""

import re
import json
import base64
import http.client
import urllib.error
import urllib.request

from dataclasses import dataclass


class ChatCompletionsError(RuntimeError):
    pass


def post_chat_completions(base_url: str, body: dict, api_key: str, timeout_s: float) -> dict:
    headers = {'Content-Type': 'application/json'}

    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    request = urllib.request.Request(
        base_url.rstrip('/') + '/chat/completions',
        data=json.dumps(body).encode(),
        headers=headers
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ChatCompletionsError(f'HTTP {error.code}: {error.read()[:300]!r}') from error
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as error:   # URLError, timeouts, resets are OSError
        raise ChatCompletionsError(f'request failed: {error}') from error


FENCE_PATTERN = re.compile(r'^```(?:json)?|```$', re.MULTILINE)


class VlmClientError(RuntimeError):
    pass


@dataclass
class VlmClientConfig:
    base_url:   str = 'http://vector-thor:8090/v1'    # any OpenAI-compatible server: llama-server, vLLM, SGLang
    model:      str = 'local'
    api_key:    str = ''
    timeout_s:  float = 10.0
    max_tokens: int = 64


class VlmClient:
    """Ask one closed question about one or more images; the server's JSON schema keeps the answer to the choices"""

    def __init__(self, config: VlmClientConfig | None = None):
        self.config = config or VlmClientConfig()

    def ask(self, jpegs: list[bytes], question: str, choices: list[str]) -> str:
        images = [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode()}}
                  for jpeg in jpegs]
        schema = {
            'type':                 'object',
            'properties':           {'answer': {'type': 'string', 'enum': choices}},
            'required':             ['answer'],
            'additionalProperties': False,
        }
        request = {
            'model':       self.config.model,
            'max_tokens':  self.config.max_tokens,
            'temperature': 0,
            'messages': [{
                'role':    'user',
                'content': [
                    *images,
                    {'type': 'text', 'text': f'{question}\nAnswer with one of: {", ".join(choices)}.'},
                ],
            }],
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'answer', 'schema': schema, 'strict': True}},
        }

        try:
            reply = post_chat_completions(self.config.base_url, request, self.config.api_key, self.config.timeout_s)
            text  = reply['choices'][0]['message']['content']
        except ChatCompletionsError as error:
            raise VlmClientError(f'VLM {error}') from error
        except (KeyError, IndexError, TypeError) as error:
            raise VlmClientError(f'VLM reply has no answer: {error}') from error

        return self._parse_answer(text, choices)

    @staticmethod
    def _parse_answer(text: str, choices: list[str]) -> str:
        payload = FENCE_PATTERN.sub('', text).strip()             # some servers wrap JSON in a ``` fence

        try:
            answer = json.loads(payload)['answer']
        except (json.JSONDecodeError, KeyError, TypeError):
            answer = text                               # a server that ignored the schema

        answer = str(answer).strip().strip('."\'').lower()

        if answer in choices:
            return answer

        matches = [choice for choice in choices if choice in answer]

        if len(matches) == 1:
            return matches[0]

        raise VlmClientError(f'answer "{text}" is not one of {choices}')
