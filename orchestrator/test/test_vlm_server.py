import io
import json
import base64
import threading
import urllib.request

from http.server import ThreadingHTTPServer

from PIL import Image

import pytest

from orchestrator.vlm import client
from orchestrator.vlm.server import SingleImage, TooManyImages, VlmAnswerer, make_handler, parse_request


def _jpeg(colour, size=(64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, colour).save(buffer, format='JPEG')
    return buffer.getvalue()


class FakeVlm:
    def __init__(self, busy_times: int = 0):
        self.busy_times = busy_times
        self.prompts    = []

    def query(self, images, prompt, max_tokens):
        if self.busy_times:
            self.busy_times -= 1
            raise RuntimeError('resident daemon busy')

        self.prompts.append(([image.shape for image in images], prompt, max_tokens))
        return 'lit'


def _request(question: str, sizes=((64, 48),)) -> dict:
    urls = ['data:image/jpeg;base64,' + base64.b64encode(_jpeg((10, 20, 30), size)).decode() for size in sizes]
    return {'model': 'local', 'max_tokens': 64, 'messages': [{'role': 'user', 'content': [
        *[{'type': 'image_url', 'image_url': {'url': url}} for url in urls], {'type': 'text', 'text': question}]}]}


def test_the_server_answers_one_image_and_waits_out_a_busy_model():
    images, text, max_tokens = parse_request(_request('Is it lit?'))
    model                   = FakeVlm(busy_times=2)
    answerer                = VlmAnswerer(model, busy_wait_s=5.0)

    assert text == 'Is it lit?' and max_tokens == 64
    assert answerer.answer(images, text, max_tokens) == 'lit'
    assert model.prompts[0][0] == [(48, 64, 3)]


def test_several_images_reach_the_model_in_order():
    images, text, max_tokens = parse_request(_request('Which frame shows the light?', sizes=((64, 48), (32, 24))))
    model                    = FakeVlm()

    assert VlmAnswerer(model).answer(images, text, max_tokens) == 'lit'
    assert model.prompts[0][0] == [(48, 64, 3), (24, 32, 3)]


def test_a_one_image_model_refuses_more_and_gets_its_one_image():
    class OneImage:
        def __init__(self):
            self.shapes = []

        def query(self, image, prompt, max_tokens):
            self.shapes.append(image.shape)
            return 'lit'

    inner    = OneImage()
    answerer = VlmAnswerer(SingleImage(inner))
    one, _, _ = parse_request(_request('q'))
    two, _, _ = parse_request(_request('q', sizes=((64, 48), (32, 24))))

    assert answerer.answer(one, 'q', 64) == 'lit' and inner.shapes == [(48, 64, 3)]

    with pytest.raises(TooManyImages, match='at most 1 image'):
        answerer.answer(two, 'q', 64)


def test_the_server_speaks_chat_completions():
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(VlmAnswerer(FakeVlm())))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                                         data=json.dumps(_request('Is it lit?')).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            reply = json.loads(response.read())
    finally:
        server.shutdown()

    assert reply['choices'][0]['message']['content'] == 'lit'


def test_the_client_sends_every_image_in_order(monkeypatch):
    sent = []

    def post(base_url, body, api_key, timeout_s):
        sent.append(body)
        return {'choices': [{'message': {'content': '{"answer": "open"}'}}]}

    monkeypatch.setattr(client, 'post_chat_completions', post)

    assert client.VlmClient().ask([_jpeg((1, 2, 3)), _jpeg((4, 5, 6))], 'Is the door open?', ['open', 'closed']) == 'open'

    parts = sent[0]['messages'][0]['content']
    assert [part['type'] for part in parts] == ['image_url', 'image_url', 'text']
