import re
import json
import base64

from dataclasses import dataclass

from .common.chat_completions import ChatCompletionsError, post_chat_completions


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
    """Ask one closed question about one image; the server's JSON schema keeps the answer to the choices"""

    def __init__(self, config: VlmClientConfig | None = None):
        self.config = config or VlmClientConfig()

    def ask(self, jpeg_bytes: bytes, question: str, choices: list[str]) -> str:
        image_url = 'data:image/jpeg;base64,' + base64.b64encode(jpeg_bytes).decode()
        schema    = {
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
                    {'type': 'image_url', 'image_url': {'url': image_url}},
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
