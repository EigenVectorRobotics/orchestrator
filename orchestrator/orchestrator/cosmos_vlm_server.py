"""cosmos_vlm_server: an OpenAI-compatible /v1/chat/completions endpoint on the Thor, answered by the resident Cosmos daemon.

The executor's VLM conditions (button_lit, door_open, ask_vlm) post one image and one closed question to
vlm_base_url. On the Thor the model is the Edge-LLM Cosmos-Reason2 daemon, which the vlm package reaches through its
file queue (vlm.models.EdgeLlmVlm) and which serves one request at a time, also for ground_server. This server
answers each request with one daemon query, and waits while the daemon is busy.

Run it on the Thor, in the environment that runs ground_server (it needs the vlm package and the daemon):

  ros2 run orchestrator cosmos_vlm_server --port 8090

Only the first image and the text of the last user message are used; response_format is not enforced, so the
question itself must name the answers (the executor's VLM client does).
"""

import io
import re
import json
import time
import base64
import argparse
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np


DATA_URL = re.compile(r'^data:image/[a-z]+;base64,(.*)$', re.DOTALL)


class CosmosAnswerer:
    """One closed question about one image, through the daemon; retries while it is busy"""

    def __init__(self, model=None, busy_wait_s: float = 20.0, max_tokens: int = 64):
        if model is None:
            from vlm.models import EdgeLlmConfig, EdgeLlmVlm       # on the Thor, from the vlm package

            model = EdgeLlmVlm(EdgeLlmConfig())

        self.model       = model
        self.busy_wait_s = busy_wait_s
        self.max_tokens  = max_tokens
        self._lock       = threading.Lock()             # one request at a time from this server

    def answer(self, jpeg: bytes, prompt: str, max_tokens: int) -> str:
        from PIL import Image

        image    = np.asarray(Image.open(io.BytesIO(jpeg)).convert('RGB'))
        deadline = time.monotonic() + self.busy_wait_s

        with self._lock:
            while True:
                try:
                    return self.model.query(image, prompt, max_tokens=min(max_tokens, self.max_tokens))
                except Exception as error:              # VlmError from the vlm package; busy is the one to wait out
                    if 'busy' not in str(error) or time.monotonic() > deadline:
                        raise

                    time.sleep(0.2)


def parse_request(body: dict) -> tuple[bytes, str, int]:
    """The first image and the text of the last user message"""
    message = next(message for message in reversed(body['messages']) if message.get('role') == 'user')
    content = message['content'] if isinstance(message['content'], list) else [{'type': 'text', 'text': message['content']}]
    images  = [part['image_url']['url'] for part in content if part.get('type') == 'image_url']
    text    = '\n'.join(part['text'] for part in content if part.get('type') == 'text')

    if not images:
        raise ValueError('the request has no image')

    match = DATA_URL.match(images[0])

    if match is None:
        raise ValueError('the image must be a base64 data URL')

    return base64.b64decode(match.group(1)), text, int(body.get('max_tokens') or 64)


def make_handler(answerer: CosmosAnswerer):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if not self.path.rstrip('/').endswith('/chat/completions'):
                return self._reply(404, {'error': f'no route {self.path}'})

            try:
                body                    = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                image, text, max_tokens = parse_request(body)
            except (ValueError, KeyError, TypeError, StopIteration, json.JSONDecodeError) as error:
                return self._reply(400, {'error': f'bad request: {error}'})

            try:
                answer = answerer.answer(image, text, max_tokens)
            except Exception as error:                  # the daemon is down, busy too long, or failed
                return self._reply(503, {'error': f'cosmos: {error}'})

            self._reply(200, {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}]})

        def do_GET(self):
            self._reply(200, {'data': [{'id': 'cosmos', 'object': 'model'}]})

        def _reply(self, code: int, payload: dict):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description='OpenAI-compatible endpoint for the Cosmos daemon on the Thor')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--busy-wait', type=float, default=20.0, help='seconds to wait while the daemon serves another request')
    arguments = parser.parse_args()

    server = ThreadingHTTPServer((arguments.host, arguments.port), make_handler(CosmosAnswerer(busy_wait_s=arguments.busy_wait)))
    print(f'cosmos_vlm_server on {arguments.host}:{arguments.port}', flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
