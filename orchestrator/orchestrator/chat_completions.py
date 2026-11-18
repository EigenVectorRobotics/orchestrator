"""One POST to an OpenAI-compatible /chat/completions endpoint, used by the VLM client"""

import json
import http.client
import urllib.error
import urllib.request


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
