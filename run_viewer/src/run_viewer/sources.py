"""Turn the orchestrator trace into a stream of viewer events."""

import json
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from run_viewer.store import EventStore


MAX_DETAIL  = 6000
RUNS        = '~/orch_runs'                 # the recorder's session folders on the robot host (--runs)
SESSIONS    = 10                            # sessions the page offers to open
IMAGES      = ('.jpg', '.jpeg', '.png')
LOCAL       = 'local'                       # --robot local: the viewer runs on the robot host itself
SSH         = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-o', 'ServerAliveInterval=5']
SCRIPTS     = Path(__file__).resolve().parents[2] / 'scripts'   # the echo scripts, sent to the robot or camera host as python3 -c
STREAMS: list[subprocess.Popen] = []    # the streams that run, stopped when the viewer stops
RESTART_S   = 5.0                       # wait before an echo that stopped starts again
NEWER_S     = 15.0                      # how often to look for a newer recorder session
MAPS_S      = 120.0                     # time for the robot host to read the prior map and make the floor maps
CALL_WATCH  = '''
import glob, json, os, sys, time
seen, parent = {}, os.getppid()
while os.getppid() == parent:
    for path in glob.glob(os.path.join(os.path.expanduser(sys.argv[1]), 'runs', '*', 'agent', '*', 'summary.json')):
        try:
            stamp = os.stat(path).st_mtime_ns
            if seen.get(path) == stamp:
                continue
            record = json.load(open(path))
        except (OSError, ValueError):
            continue
        seen[path] = stamp
        parts = path.split(os.sep)
        record.setdefault('call', parts[-2])
        record['run'] = parts[-4]
        print(json.dumps(record, default=str), flush=True)
    if len(sys.argv) > 2:
        break
    time.sleep(0.25)
'''                                         # the recorder writes <session>/runs/<run>/agent/<call>/summary.json
CALL_DETAIL = '''
import glob, json, os, sys
session, call = os.path.expanduser(sys.argv[1]), sys.argv[2]
out = {'run': None, 'request': None, 'messages': [], 'summary': None}
def load(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None
for folder in sorted(glob.glob(os.path.join(session, 'runs', '*', 'agent', call))):
    out['run'] = folder.split(os.sep)[-3]
    out['request'], out['summary'] = load(os.path.join(folder, 'request.json')), load(os.path.join(folder, 'summary.json'))
    try:
        lines = open(os.path.join(folder, 'messages.jsonl')).read().splitlines()
    except OSError:
        lines = []
    for line in filter(None, lines):
        try:
            out['messages'].append(json.loads(line))
        except ValueError:
            out['messages'].append({'kind': 'unreadable', 'text': line})
print(json.dumps(out, default=str))
'''                                         # one call's request.json, messages.jsonl and summary.json


def clip(
    text: str,
    limit: int = MAX_DETAIL,
) -> str:
    """Cut long text and say how much was cut."""
    return text if len(text) <= limit else text[:limit] + f'\n... ({len(text) - limit} more characters)'


def trace_event(
    raw: dict,
    last_epoch: float,
) -> dict | None:
    """Make a viewer event from one orchestrator trace event; a monotonic time takes the last epoch time seen."""
    kind = raw.get('kind', '')
    stamp = float(raw.get('received') or raw.get('time') or time.time())
    stamp = stamp if stamp > 1e9 else last_epoch
    text = str(raw.get('text', ''))
    status = raw.get('status')
    if raw.get('source') == 'planner':
        return {'kind': 'chat', 'sub': kind, 'title': clip(text), 'time': stamp}
    if kind == 'node':
        return {'kind': 'node', 'title': text, 'time': stamp, 'status': status,
                'path': raw.get('path', ''), 'node': raw.get('node', '')}
    if kind == 'record':
        record = raw.get('record') or {}
        return {'kind': 'record', 'title': text, 'time': stamp, 'path': record.get('path', ''), 'status': record.get('status'),
                'button': (record.get('inputs') or {}).get('target_description')}
    if kind in ('started', 'finished', 'halted', 'error'):
        return {'kind': 'tree', 'title': text, 'time': stamp, 'xml': raw.get('tree'), 'tree_id': raw.get('tree_id'),
                'status': status or ('running' if kind == 'started' else kind)}
    if kind.startswith('agent_'):
        answer = raw.get('decision') if isinstance(raw.get('decision'), dict) else None
        return {'kind': kind, 'title': clip(text), 'time': stamp, 'phase': raw.get('phase'),
                'decision': answer.get('decision') if answer else None, 'tool': raw.get('tool'), 'call': raw.get('call'),
                'arguments': raw.get('arguments'), 'answer': answer}
    return None


def host_command(
    host: str,
    command: str,
    tty: bool = True,
) -> list[str]:
    """The argv that runs a shell command on host: 'local' runs it here, 'a,b' runs it on b by ssh from a, any other host over ssh."""
    if host == LOCAL:
        return ['bash', '-c', command]
    ssh = SSH + ['-tt'] * tty                   # -tt: a remote command that runs on gets a hangup, and stops, when its ssh stops
    first, _, rest = host.partition(',')
    if rest:                                    # the Orin takes only the NUC's key: hop through the NUC
        command = f'{shlex.join(ssh)} {rest} {shlex.quote(command)}'
    return [*ssh, first, command]


def run_on(
    host: str,
    command: str,
) -> subprocess.Popen:
    """Start a shell command on host with its output lines on stdout; stdin stays off the terminal, which ssh -tt would take."""
    proc = subprocess.Popen(host_command(host, command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, bufsize=1)
    STREAMS[:] = [p for p in STREAMS if p.poll() is None] + [proc]
    return proc


def stop_streams() -> None:
    """Stop every stream; ssh -tt then stops its remote command too."""
    for proc in STREAMS:
        proc.terminate()


def run_python(
    host: str,
    ros_env: str,
    script: str,
    *args: str,
) -> subprocess.Popen:
    """Start one of the echo scripts on host as python3 -c, in its ROS environment: the host needs no copy of it."""
    code = shlex.quote((SCRIPTS / script).read_text())
    return run_on(host, f'source {ros_env} >/dev/null 2>&1; exec env -u LD_PRELOAD python3 -c {code} {" ".join(args)}')


def call_watch(
    host: str,
    session: str,
    once: bool = False,
) -> subprocess.Popen:
    """Start the watcher of the agent call summaries of a session folder on host; once: read them once and stop."""
    return run_on(host, f'python3 -c {shlex.quote(CALL_WATCH)} {session}' + (' once' if once else ''))


def json_lines(
    proc: subprocess.Popen,
) -> Iterator[dict]:
    """The JSON lines a stream prints, until it stops; other lines are skipped."""
    for line in proc.stdout:
        try:
            yield json.loads(line)
        except ValueError:
            continue


def echo_items(
    host: str,
    ros_env: str,
    script: str,
    *args: str,
) -> Iterator[dict]:
    """The JSON lines of an echo script on host, without end: the echo starts again when it stops."""
    while True:
        proc = run_python(host, ros_env, script, *args)
        yield from json_lines(proc)
        proc.wait()
        time.sleep(RESTART_S)


def add_trace(
    store: EventStore,
    raw: dict,
    last_epoch: float,
) -> float:
    """Add the viewer event of one trace event to the store; return the last epoch time seen."""
    event = trace_event(raw, last_epoch)
    if event is None:
        return last_epoch
    store.add(event)
    return event['time']


def remote_newest(
    host: str,
) -> str:
    """Return the newest recorder events.jsonl path on the robot host; raise ConnectionError when ssh cannot reach it."""
    out = subprocess.run(host_command(host, f'ls -t {RUNS}/*/events.jsonl 2>/dev/null | head -1', tty=False), capture_output=True, text=True, timeout=20)   # no tty: stderr stays out of the path
    if out.returncode == 255:                   # ssh's own failure, not the remote command's
        raise ConnectionError((out.stderr.strip().splitlines() or ['ssh failed'])[-1])
    return out.stdout.strip()


def follow_trace(
    host: str,
    store: EventStore,
    fixed_path: str | None = None,
) -> None:
    """Follow a trace file over ssh: fixed_path, or the newest recorder events.jsonl, switching when a newer one appears."""
    read: dict[str, int] = {}                   # lines read per file: a new tail after an ssh drop starts after them
    last_epoch = 0.0
    while True:
        try:
            path = fixed_path or remote_newest(host)
            missing = f'no events.jsonl on {host}; is run_recorder running?'
        except (subprocess.TimeoutExpired, ConnectionError) as error:
            path, missing = '', f'cannot reach {host}: {error}'
        if not path:
            store.set_status(missing)
            time.sleep(RESTART_S)
            continue
        store.set_status(f'{host}:{path}')
        lines = read.get(path, 0)
        if not lines:
            store.add({'kind': 'session', 'path': path, 'time': time.time()})
        proc = run_on(host, f'tail -n +{lines + 1} -F {path}')
        calls = call_watch(host, path.rsplit('/', 1)[0])
        threading.Thread(target=follow_calls, args=(calls, store), daemon=True).start()
        if not fixed_path:
            threading.Thread(target=stop_on_newer, args=(host, path, proc), daemon=True).start()
        for raw in json_lines(proc):
            lines += 1
            last_epoch = add_trace(store, raw, last_epoch)
        read[path] = lines
        proc.wait()
        calls.terminate()
        time.sleep(2)


def follow_camera(
    host: str,
    store: EventStore,
    ros_env: str,
) -> None:
    """Follow the camera frame and mask outlines from the camera host (kept as latest)."""
    for item in echo_items(host, ros_env, 'camera_echo.py'):
        store.set_latest('camera', item)


def watch_calls(
    host: str,
    store: EventStore,
    session: str,
) -> None:
    """Follow the agent call summaries of one session folder on host (for a trace that comes from the topic)."""
    follow_calls(call_watch(host, session), store)


def follow_calls(
    proc: subprocess.Popen,
    store: EventStore,
) -> None:
    """Add one viewer event per new or changed agent call summary."""
    for record in json_lines(proc):
        stamp = float(record.get('started') or time.time())
        store.add({**record, 'kind': 'call', 'prompt': clip(str(record.get('prompt', '')), 4 * MAX_DETAIL), 'time': stamp})


def follow_live(
    host: str,
    store: EventStore,
    ros_env: str,
    trace_topic: bool = False,
) -> None:
    """Follow the nav state (kept as latest), the agent request and reply, and with trace_topic the trace."""
    if trace_topic:
        store.set_status(f'{host}: /orchestrator/trace (topic)')
    last_epoch = 0.0
    for item in echo_items(host, ros_env, 'live_echo.py', *(['--trace'] if trace_topic else [])):
        name = item.pop('name', '')
        if name == 'nav':
            store.set_latest(name, item)
        elif name == 'trace':
            last_epoch = add_trace(store, item['raw'], last_epoch)
        elif name:
            store.add({'kind': name, 'call': item.pop('id', None), **item})   # the id of a request or reply is the agent call id


def stop_on_newer(
    host: str,
    path: str,
    proc: subprocess.Popen,
) -> None:
    """Stop the tail when the robot host has a newer events.jsonl."""
    while proc.poll() is None:
        time.sleep(NEWER_S)
        try:
            newest = remote_newest(host)
        except (subprocess.TimeoutExpired, ConnectionError):
            continue
        if newest and newest != path:
            proc.terminate()
            return


def follow_states(
    host: str,
    store: EventStore,
    ros_env: str,
) -> None:
    """Follow the elevator side and door state on the robot host."""
    for state in echo_items(host, ros_env, 'state_echo.py'):
        store.add({'kind': 'state', **state})


def load_trace(
    host: str,
    path: str,
    store: EventStore,
) -> None:
    """Read a finished session's trace and agent call summaries once."""
    store.set_status(f'{host}:{path}')
    store.add({'kind': 'session', 'path': path, 'time': time.time()})
    last_epoch = 0.0
    proc = run_on(host, f'cat {path}')
    for raw in json_lines(proc):
        last_epoch = add_trace(store, raw, last_epoch)
    proc.wait()
    follow_calls(call_watch(host, path.rsplit('/', 1)[0], once=True), store)


class Sessions:
    """The recent sessions on the robot host, each loaded into its own store when first asked for."""

    def __init__(
        self,
        host: str,
        live: EventStore,
    ) -> None:
        """Keep the live store; others load on demand."""
        self.host = host
        self.live = live
        self.stores: dict[str, EventStore] = {}
        self.floor_maps: dict[str, dict] = {}   # per places file path and hash
        self.lock = threading.Lock()

    def run(
        self,
        command: str,
        timeout_s: float = 20.0,
    ) -> bytes:
        """Run a shell command on the robot host and return its output."""
        return subprocess.run(host_command(self.host, command, tty=False), capture_output=True, timeout=timeout_s).stdout

    def list(self) -> list[dict]:
        """The newest sessions with an events.jsonl, newest first."""
        out = self.run(f'ls -t {RUNS}/*/events.jsonl 2>/dev/null | head -{SESSIONS} | xargs -r stat -c "%Y %s %n"').decode()
        rows = [line.split(' ', 2) for line in out.splitlines() if line.count(' ') >= 2]
        return [{'name': path.rsplit('/', 2)[-2], 'path': path, 'mtime': int(mtime), 'size': int(size), 'live': i == 0}
                for i, (mtime, size, path) in enumerate(rows)]

    def store(
        self,
        name: str | None,
    ) -> EventStore | None:
        """The store for a session name; the live store for none or the newest."""
        if not name:
            return self.live
        found = [s for s in self.list() if s['name'] == name]
        if not found:
            return None
        if found[0]['live']:
            return self.live
        with self.lock:
            if name not in self.stores:
                self.stores[name] = EventStore()
                threading.Thread(target=load_trace, args=(self.host, found[0]['path'], self.stores[name]), daemon=True).start()
            return self.stores[name]

    def call(
        self,
        name: str,
        call: str,
    ) -> dict | None:
        """One agent call of a session: its run, request, messages and summary."""
        if not re.fullmatch(r'[\w.-]+', name or '') or not re.fullmatch(r'[\w-]+', call or ''):
            return None
        out = self.run(f'python3 -c {shlex.quote(CALL_DETAIL)} {RUNS}/{name} {call}')
        try:
            detail = json.loads(out, parse_constant=str)
        except ValueError:
            return None
        return detail if detail.get('run') else None

    def maps(
        self,
        name: str,
    ) -> dict | None:
        """The floor maps of the places file that a session's provenance.json names, made on the robot host once per places file."""
        if not re.fullmatch(r'[\w.-]+', name or ''):
            return None
        try:
            places = json.loads(self.run(f'cat {RUNS}/{name}/provenance.json')).get('places') or {}
        except ValueError:
            return None
        if not places.get('path'):
            return None
        key = f'{places["path"]}:{places.get("sha256")}'
        if key not in self.floor_maps:
            out = self.run(f'python3 -c {shlex.quote((SCRIPTS / "make_floor_maps.py").read_text())} {shlex.quote(places["path"])}', MAPS_S)
            try:
                self.floor_maps[key] = json.loads(out)
            except ValueError:
                return None
        return self.floor_maps[key]

    def frame(
        self,
        name: str,
        call: str,
        path: str,
    ) -> bytes | None:
        """An image the call's request names; the recorder moved it into the run's captures/ when the run ended."""
        detail = self.call(name, call)
        if not detail or not path.lower().endswith(IMAGES) or path not in json.dumps(detail.get('request'), default=str):
            return None
        kept = f'{RUNS}/{name}/runs/{detail["run"]}/captures/{path.rsplit("/", 1)[-1]}'
        return self.run(f'cat {home(kept)} 2>/dev/null || cat {home(path)}') or None


def home(
    path: str,
) -> str:
    """A path quoted for the shell, with a leading ~/ kept as the home folder."""
    return '"$HOME"/' + shlex.quote(path[2:]) if path.startswith('~/') else shlex.quote(path)
