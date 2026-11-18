import io
import json
import base64
import threading
import urllib.request

from http.server import ThreadingHTTPServer

from PIL import Image

from orchestrator.vlm.cosmos_server import CosmosAnswerer, make_handler, parse_request


def _jpeg(colour, size=(64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, colour).save(buffer, format='JPEG')
    return buffer.getvalue()


class FakeCosmos:
    def __init__(self, busy_times: int = 0):
        self.busy_times = busy_times
        self.prompts    = []

    def query(self, image, prompt, max_tokens):
        if self.busy_times:
            self.busy_times -= 1
            raise RuntimeError('resident daemon busy')

        self.prompts.append((image.shape, prompt, max_tokens))
        return 'lit'


def _request(question: str) -> dict:
    url = 'data:image/jpeg;base64,' + base64.b64encode(_jpeg((10, 20, 30))).decode()
    return {'model': 'local', 'max_tokens': 64, 'messages': [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': url}}, {'type': 'text', 'text': question}]}]}


def test_the_cosmos_server_answers_one_image_and_waits_out_a_busy_daemon():
    image, text, max_tokens = parse_request(_request('Is it lit?'))
    model                   = FakeCosmos(busy_times=2)
    answerer                = CosmosAnswerer(model, busy_wait_s=5.0)

    assert text == 'Is it lit?' and max_tokens == 64
    assert answerer.answer(image, text, max_tokens) == 'lit'
    assert model.prompts[0][0] == (48, 64, 3)


def test_the_cosmos_server_speaks_chat_completions():
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(CosmosAnswerer(FakeCosmos())))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                                         data=json.dumps(_request('Is it lit?')).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            reply = json.loads(response.read())
    finally:
        server.shutdown()

    assert reply['choices'][0]['message']['content'] == 'lit'
