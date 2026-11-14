"""HTTP server: the page at / and a server-sent event stream at /stream."""

import json
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from run_viewer.store import EventStore


PAGE = Path(__file__).parent / 'static' / 'index.html'
MAPS = Path(__file__).parent / 'static' / 'maps'
TYPES = {'.png': 'image/png', '.json': 'application/json'}
MAX_BATCH = 500                   # events per stream message: a page that loads a long session gets them in parts
BOOT  = str(time.time())          # new at each start: a page that sees it change reloads, as its event numbers are of the old server


class Handler(BaseHTTPRequestHandler):
    """Serve the page and stream events."""

    store: EventStore = None

    def log_message(
        self,
        format: str,
        *args: object,
    ) -> None:
        """Keep the terminal quiet."""

    def do_GET(self) -> None:
        """Route / to the page and /stream to the event stream."""
        url = urlparse(self.path)
        if url.path == '/':
            self.send_body(PAGE.read_bytes(), 'text/html; charset=utf-8')
        elif url.path == '/stream':
            self.stream(int(parse_qs(url.query).get('from', ['0'])[0]))
        elif url.path == '/latest':
            self.send_body(json.dumps(self.store.latest, default=str).encode(), 'application/json')
        elif url.path.startswith('/maps/') and (MAPS / Path(url.path).name).is_file():
            file = MAPS / Path(url.path).name
            self.send_body(file.read_bytes(), TYPES.get(file.suffix, 'application/octet-stream'))
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
        seq: int,
    ) -> None:
        """Send events from seq on as server-sent events until the client leaves."""
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
            while True:
                batch = self.store.since(seq, 15.0)[:MAX_BATCH]
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
    store: EventStore,
    host: str,
    port: int,
) -> None:
    """Serve forever on host:port."""
    Handler.store = store
    ThreadingHTTPServer((host, port), Handler).serve_forever()
