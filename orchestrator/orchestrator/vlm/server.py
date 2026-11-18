"""vlm_server: an OpenAI-compatible /v1/chat/completions endpoint in front of any VLM with query(images, prompt, max_tokens).

The executor's VLM conditions (door_open_vlm, ask_vlm) post images and one closed question to vlm_base_url. This
server answers each request with one query, one at a time, and waits while the model says it is busy. A model that
takes fewer images than a request has (max_images) refuses it with a 400. A model is an
entry of MODELS; edgellm is the Thor's Edge-LLM Cosmos-Reason2 daemon, which the vlm package reaches through its file
queue (vlm.models.EdgeLlmVlm) and which also serves ground_server.

Run it on the Thor, in the environment that runs ground_server (edgellm needs the vlm package and the daemon):

  ros2 run orchestrator vlm_server --port 8090

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


class TooManyImages(ValueError):
    pass


class SingleImage:
    """A model whose query takes one image, behind the list interface"""

    max_images = 1

    def __init__(self, model):
        self.model = model

    def query(self, images: list, prompt: str, max_tokens: int) -> str:
        return self.model.query(images[0], prompt, max_tokens=max_tokens)


def edgellm_model():
    """The Thor's Edge-LLM daemon, one image per query; the vlm package exists only on the Thor, so it is imported here"""
    from vlm.models import EdgeLlmConfig, EdgeLlmVlm

    return SingleImage(EdgeLlmVlm(EdgeLlmConfig()))


MODELS = {'edgellm': edgellm_model}            # --model name -> a factory of a model with query(images, prompt, max_tokens)


class VlmAnswerer:
    """One closed question about one or more images, through the model; retries while it says it is busy"""

    def __init__(self, model, busy_wait_s: float = 20.0, max_tokens: int = 64):
        self.model       = model
        self.busy_wait_s = busy_wait_s
        self.max_tokens  = max_tokens
        self._lock       = threading.Lock()             # one request at a time from this server

    def answer(self, jpegs: list[bytes], prompt: str, max_tokens: int) -> str:
        from PIL import Image

        limit = getattr(self.model, 'max_images', None)

        if limit is not None and len(jpegs) > limit:
            raise TooManyImages(f'the model takes at most {limit} image(s), the request has {len(jpegs)}')

        images   = [np.asarray(Image.open(io.BytesIO(jpeg)).convert('RGB')) for jpeg in jpegs]
        deadline = time.monotonic() + self.busy_wait_s

        with self._lock:
            while True:
                try:
                    return self.model.query(images, prompt, max_tokens=min(max_tokens, self.max_tokens))
                except Exception as error:              # a busy model is the one error to wait out
                    if 'busy' not in str(error) or time.monotonic() > deadline:
                        raise

                    time.sleep(0.2)


def parse_request(body: dict) -> tuple[list[bytes], str, int]:
    """Every image and the text of the last user message, in order"""
    message = next(message for message in reversed(body['messages']) if message.get('role') == 'user')
    content = message['content'] if isinstance(message['content'], list) else [{'type': 'text', 'text': message['content']}]
    images  = [part['image_url']['url'] for part in content if part.get('type') == 'image_url']
    text    = '\n'.join(part['text'] for part in content if part.get('type') == 'text')

    if not images:
        raise ValueError('the request has no image')

    matches = [DATA_URL.match(image) for image in images]

    if any(match is None for match in matches):
        raise ValueError('each image must be a base64 data URL')

    return [base64.b64decode(match.group(1)) for match in matches], text, int(body.get('max_tokens') or 64)


def make_handler(answerer: VlmAnswerer, model_name: str = 'vlm'):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if not self.path.rstrip('/').endswith('/chat/completions'):
                return self._reply(404, {'error': f'no route {self.path}'})

            try:
                body                    = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                images, text, max_tokens = parse_request(body)
            except (ValueError, KeyError, TypeError, StopIteration, json.JSONDecodeError) as error:
                return self._reply(400, {'error': f'bad request: {error}'})

            try:
                answer = answerer.answer(images, text, max_tokens)
            except TooManyImages as error:
                return self._reply(400, {'error': f'bad request: {error}'})
            except Exception as error:                  # the model is down, busy too long, or failed
                return self._reply(503, {'error': f'{model_name}: {error}'})

            self._reply(200, {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}]})

        def do_GET(self):
            self._reply(200, {'data': [{'id': model_name, 'object': 'model'}]})

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
    parser = argparse.ArgumentParser(description='OpenAI-compatible endpoint in front of a VLM (default: the Edge-LLM daemon on the Thor)')
    parser.add_argument('--model', choices=sorted(MODELS), default='edgellm')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--busy-wait', type=float, default=20.0, help='seconds to wait while the model serves another request')
    arguments = parser.parse_args()

    answerer = VlmAnswerer(MODELS[arguments.model](), busy_wait_s=arguments.busy_wait)
    server   = ThreadingHTTPServer((arguments.host, arguments.port), make_handler(answerer, arguments.model))
    print(f'vlm_server ({arguments.model}) on {arguments.host}:{arguments.port}', flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
