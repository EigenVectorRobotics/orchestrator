"""The agent backend: the executor sends each call, evidence and frames included, to a bridge on the laptop
(mission_control/agent_bridge.py) over one TCP connection it keeps open, and waits for the decision on it. The bridge
asks the model for press_judge and manipulation_recovery, and the operator in its terminal for operator_confirm.

A message is a header of two big-endian uint32 (the JSON length, then the length of the binary after it), the JSON,
then the images back to back, as its "images" list gives them ({name, media_type, size}). The executor sends call,
reply (accepted or why not) and cancel; the bridge sends started (model and prompt), decision and failed. Both send
ping every PING_S; a link silent for DEAD_S is closed and opened again, and the open call is sent again on it.

Each call is also published as a request (latched JSON on topics.AGENT_REQUEST_TOPIC: opened, then closed) and each reply on
topics.AGENT_REPLY_TOPIC, for the run recorder and the Run Viewer.
"""

import json
import time
import uuid
import queue
import socket
import struct
import threading

from typing import Callable
from pathlib import Path

from .agent import PHASE_DECISIONS, PRESS_PHASES, STANCE_PHASES, AgentError, AgentSession


DECISION_FIELDS = ('decision', 'reason', 'dx', 'dy', 'dyaw', 'use_left_arm', 'ox', 'oy', 'oz')
HEADER      = struct.Struct('!II')
MAX_JSON    = 16_000_000
MAX_BINARY  = 64_000_000
PING_S      = 5.0
DEAD_S      = 20.0
RETRY_S     = 2.0
MAX_REFUSED = 3                         # decisions refused in one call before it fails
RECORDS     = Path.home() / 'orch_runs'


class AgentStopped(AgentError):
    pass


class LinkClosed(ConnectionError):
    pass


def open_request(skill: str, phase: str, task: str, evidence: dict) -> dict:
    """The call as the bridge gets it: the task, the allowed decisions and all the evidence"""
    request = {
        'id':        uuid.uuid4().hex[:8],
        'state':     'open',
        'skill':     skill,
        'opened':    time.time(),
        'task':      task,
        'decisions': list(PHASE_DECISIONS[phase]),
        'failure':   evidence.get('last_failure') or None,
        'history':   evidence.get('history') or [],
    }

    if phase in PRESS_PHASES:
        request['press'] = evidence.get('press') or None

    if phase in STANCE_PHASES:
        request['stance'] = evidence.get('stance') or None

    return request


def send_message(sock: socket.socket, header: dict, blobs: list[bytes] = ()):
    data = json.dumps(header, default=str).encode()
    sock.sendall(HEADER.pack(len(data), sum(map(len, blobs))) + data + b''.join(blobs))


def _read_exactly(sock: socket.socket, size: int) -> bytes:
    chunks, left = [], size

    while left:
        chunk = sock.recv(min(left, 1 << 20))

        if not chunk:
            raise LinkClosed('the bridge closed the connection')

        chunks.append(chunk)
        left -= len(chunk)

    return b''.join(chunks)


def read_message(sock: socket.socket) -> tuple[dict, list[bytes]] | None:
    """The next message, or None when none began within the socket's timeout"""
    try:
        first = sock.recv(1)
    except socket.timeout:
        return None

    if not first:
        raise LinkClosed('the bridge closed the connection')

    sizes = HEADER.unpack(first + _read_exactly(sock, HEADER.size - 1))

    if sizes[0] > MAX_JSON or sizes[1] > MAX_BINARY:
        raise LinkClosed(f'a message of {sizes} bytes is too large')

    header, binary = json.loads(_read_exactly(sock, sizes[0])), _read_exactly(sock, sizes[1])
    blobs, start   = [], 0

    for image in header.get('images', []):
        blobs.append(binary[start:start + image['size']])
        start += image['size']

    return header, blobs


class PushLink:
    """One TCP connection to the bridge, opened in the background and opened again when it drops. Messages from
    the bridge go to messages; the open call is sent again on each new connection"""

    def __init__(self, host: str, port: int, on_state: Callable[[str], None] | None = None):
        self.host     = host
        self.port     = port
        self.messages = queue.Queue()
        self.on_state = on_state or (lambda text: None)
        self._sock    = None
        self._call    = None
        self._lock    = threading.Lock()
        self._closed  = threading.Event()
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def send(self, header: dict, blobs: list[bytes] = ()) -> bool:
        with self._lock:
            sock = self._sock

            if sock is None:
                return False

            try:
                send_message(sock, header, blobs)
                return True
            except OSError:
                self._drop(sock)
                return False

    def open_call(self, header: dict, blobs: list[bytes]):
        while not self.messages.empty():          # answers to calls that closed before they came
            self.messages.get_nowait()

        self._call = (header, blobs)
        self.send(header, blobs)

    def close_call(self):
        self._call = None

    def close(self):
        self._closed.set()

        with self._lock:
            if self._sock is not None:
                self._drop(self._sock)

    def _drop(self, sock: socket.socket):
        try:
            sock.close()
        except OSError:
            pass

        if self._sock is sock:
            self._sock = None

    def _run(self):
        while not self._closed.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=RETRY_S)
            except OSError:
                self._closed.wait(RETRY_S)
                continue

            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(PING_S)

            with self._lock:
                self._sock = sock

            self.on_state(f'connected to the agent bridge at {self.host}:{self.port}')

            if self._call is not None:
                self.send(*self._call)

            try:
                self._read(sock)
            except (OSError, ValueError) as error:     # a closed, reset, silent or garbled link: open it again
                self.on_state(f'the agent bridge link dropped: {error}')

            with self._lock:
                self._drop(sock)

    def _read(self, sock: socket.socket):
        heard = time.monotonic()

        while not self._closed.is_set():
            message = read_message(sock)

            if message is None:
                if time.monotonic() - heard > DEAD_S:
                    raise LinkClosed(f'nothing from the bridge for {DEAD_S:g} s')

                self.send({'type': 'ping'})
                continue

            heard = time.monotonic()

            if message[0].get('type') != 'ping':
                self.messages.put(message[0])


def call_images(request: dict, session: AgentSession) -> list[tuple[str, str, bytes]]:
    """The images a call carries, as (name, media type, bytes): the press frames it names, and for a recovery the
    camera image now"""
    images = []

    for name, path in ((request.get('press') or {}).get('frames') or {}).items():
        try:
            images.append((name, 'image/jpeg', Path(path).read_bytes()))
        except OSError:
            continue

    if request['skill'] == 'manipulation_recovery' and (camera := session.camera()) is not None:
        images.append(('camera', camera[1], camera[0]))

    return images


class PushAgentBackend:
    name = 'push'

    def __init__(self, link: PushLink, publish_request: Callable[[dict], None] = lambda request: None,
                 publish_reply: Callable[[dict], None] = lambda reply: None, records: Path = RECORDS):
        self.link             = link
        self.records          = Path(records)
        self._publish_request = publish_request
        self._publish_reply   = publish_reply

    def run(self, skill: str, phase: str, task: str, session: AgentSession,
            should_stop: Callable[[], bool], on_event: Callable[..., None]):
        # (1) The call with all its evidence and frames, sent now and again on each reconnect
        request = open_request(skill, phase, task, session.evidence)
        images  = call_images(request, session)
        header  = {**request, 'type': 'call', 'phase': phase,
                   'images': [{'name': name, 'media_type': media, 'size': len(data)} for name, media, data in images]}
        record  = {'call': request['id'], 'skill': skill, 'model': None, 'effort': None, 'started': round(request['opened'], 1),
                   'state': 'thinking', 'prompt': None, 'decision': None}
        refused = 0

        self._publish_request(request)
        self.link.open_call(header, [data for _, _, data in images])
        on_event('agent_wait', f'call {request["id"]} pushed to the agent bridge'
                 + ('' if self.link.connected else ' (not connected yet; it goes when the bridge is up)'), call=request['id'])

        try:
            while True:
                # (2) The next message for this call; a cancel or the leaf's timeout tells the bridge to drop it
                if should_stop():
                    self.link.send({'type': 'cancel', 'id': request['id']})
                    self._record({**record, 'state': 'failed', 'error': 'cancelled'})
                    raise AgentStopped('cancelled or timed out')

                try:
                    message = self.link.messages.get(timeout=0.2)
                except queue.Empty:
                    continue

                if message.get('id') != request['id']:
                    continue

                # (3) Act on it: a decision goes through decide, and a refusal goes back to the bridge
                if message['type'] == 'started':
                    record.update({key: message.get(key) for key in ('model', 'effort', 'prompt')})
                    self._record(record)

                if message['type'] == 'failed':
                    self._record({**record, 'state': 'failed', 'error': message.get('error'), **_usage(message)})
                    raise AgentError(f'the agent bridge failed: {message.get("error")}')

                if message['type'] != 'decision':
                    continue

                decision = {key: message[key] for key in DECISION_FIELDS if key in message}
                output   = session.decide(dict(decision))
                reply    = {'id': request['id'], 'accepted': output.final, 'message': output.text}
                self.link.send({'type': 'reply', **reply})
                self._publish_reply({**reply, 'decision': decision})
                record.update({'decision': decision, **_usage(message)})

                if output.final:
                    self._record({**record, 'state': 'answered'})
                    return

                refused += 1

                if refused >= MAX_REFUSED:
                    self._record({**record, 'state': 'failed', 'error': f'{refused} decisions refused: {output.text}'})
                    raise AgentError(f'{refused} decisions refused; the last: {output.text}')
        finally:
            self.link.close_call()
            self._publish_request({**request, 'state': 'closed', 'closed': time.time()})

    def _record(self, record: dict):
        """The call's record next to the newest recorder session, where the Run Viewer's Responder tile reads it"""
        sessions = sorted(self.records.glob('*/events.jsonl'), key=lambda path: path.stat().st_mtime)

        if not sessions:
            return

        folder = sessions[-1].parent / 'agent_calls'

        try:
            folder.mkdir(exist_ok=True)
            seconds = round(time.time() - record['started'], 1)
            (folder / f'{record["call"]}.json').write_text(json.dumps({**record, 'seconds': seconds}, indent=1, default=str))
        except OSError:                                 # a lost record never fails the call
            pass


def _usage(message: dict) -> dict:
    return {key: message[key] for key in ('cost_usd', 'tokens_in', 'tokens_out', 'model_s') if key in message}
