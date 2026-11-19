"""HTTP server: the page at / and a server-sent event stream at /stream."""

import json
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from run_viewer.sources import Sessions
from run_viewer.store import EventStore


PAGE = Path(__file__).parent / 'static' / 'index.html'
MAX_BATCH = 500                   # events per stream message: a page that loads a long session gets them in parts
BOOT  = str(time.time())          # new at each start: a page that sees it change reloads, as its event numbers are of the old server


class Handler(BaseHTTPRequestHandler):
    """Serve the page and stream events."""

    sessions: Sessions = None
    title: str = ''

    def log_message(
        self,
        format: str,
        *args: object,
    ) -> None:
        """Keep the terminal quiet."""

    def do_GET(self) -> None:
        """Route / to the page, /stream to the event stream, /latest to the nav and camera state,
        /sessions, /call, /frame and /maps to the recorder's session folders."""
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == '/':
            self.send_body(PAGE.read_bytes().replace(b'{{TITLE}}', self.title.encode()), 'text/html; charset=utf-8')
        elif url.path == '/stream':
            store = self.sessions.store(query.get('session'))
            self.stream(store, int(query.get('from', '0'))) if store else self.send_error(404)
        elif url.path == '/latest':
            self.send_body(json.dumps(self.sessions.live.latest, default=str).encode(), 'application/json')
        elif url.path == '/sessions':
            self.send_body(json.dumps(self.sessions.list()).encode(), 'application/json')
        elif url.path == '/call':
            detail = self.sessions.call(query.get('session', ''), query.get('id', ''))
            self.send_body(json.dumps(detail, default=str, ensure_ascii=False).encode(), 'application/json') if detail else self.send_error(404)
        elif url.path == '/frame':
            image = self.sessions.frame(query.get('session', ''), query.get('id', ''), query.get('path', ''))
            media = 'image/png' if query.get('path', '').lower().endswith('.png') else 'image/jpeg'
            self.send_body(image, media) if image else self.send_error(404)
        elif url.path == '/maps':
            maps = self.sessions.maps(query.get('session', ''))
            self.send_body(json.dumps(maps).encode(), 'application/json') if maps else self.send_error(404)
        else:
            self.send_error(404)

    def send_body(
        self,
        body: bytes,
        media_type: str,
    ) -> None:
        """Send one whole response, not cached."""
        self.send_response(200)
        self.send_header('Content-Type', media_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def stream(
        self,
        store: EventStore,
        seq: int,
    ) -> None:
        """Send events from seq on as server-sent events until the client leaves; a fresh client gets a folded snapshot first."""
        gzip = 'gzip' in self.headers.get('Accept-Encoding', '')
        packer = zlib.compressobj(6, zlib.DEFLATED, 31) if gzip else None
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        if gzip:
            self.send_header('Content-Encoding', 'gzip')
        self.end_headers()
        try:
            self.send_chunk(packer, f'event: boot\ndata: {BOOT}\n\n'.encode())
            if seq == 0:
                batch, seq = store.snapshot()
                for i in range(0, len(batch), MAX_BATCH):
                    payload = json.dumps(batch[i:i + MAX_BATCH], default=str, ensure_ascii=False)
                    self.send_chunk(packer, f'id: {seq}\ndata: {payload}\n\n'.encode())
            while True:
                batch = store.since(seq, 15.0)[:MAX_BATCH]
                if not batch:
                    self.send_chunk(packer, b': keepalive\n\n')
                    continue
                seq += len(batch)
                payload = json.dumps(batch, default=str, ensure_ascii=False)
                self.send_chunk(packer, f'id: {seq}\ndata: {payload}\n\n'.encode())
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_chunk(
        self,
        packer: 'zlib._Compress | None',
        data: bytes,
    ) -> None:
        """Send one piece of the event stream now, gzipped when the client takes gzip."""
        self.wfile.write(packer.compress(data) + packer.flush(zlib.Z_SYNC_FLUSH) if packer else data)
        self.wfile.flush()


def serve(
    sessions: Sessions,
    host: str,
    port: int,
    title: str = '',
) -> None:
    """Serve forever on host:port."""
    Handler.sessions = sessions
    Handler.title = title
    ThreadingHTTPServer((host, port), Handler).serve_forever()
