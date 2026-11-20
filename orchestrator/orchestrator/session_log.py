"""The task agent's own words, read from its Codex session log, for the Mission feed.

The task agent is a Codex session that starts in the agent's folder (agent or agent_discovery). Codex writes each
session to ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl: a session_meta line (with the cwd) and then one line per
item. This module reads two kinds of item, and nothing else:

  - reasoning: the summary of the model's reasoning. Codex writes it only when it starts with
    -c model_reasoning_summary="detailed"; else the summary is empty.
  - message:   what the agent says (role assistant).

It does not read commands, their output or the prompts: these can hold file contents, and the page shows only what
the agent thinks and says. No ROS here: the node calls new() about once a second while a mission is open.
"""

import json
import time

from datetime import date, datetime, timedelta
from pathlib import Path


CODEX_SESSIONS = Path.home() / '.codex' / 'sessions'
MAX_TEXT       = 2000               # characters of one item; a longer item is cut
MAX_READ       = 4 * 1024 * 1024    # bytes of one file in one call: a long log is read over a few calls
SEEN_KEEP      = 5000               # texts kept to drop a copy (a forked worker's log starts with its parent's items)
DAYS_BACK      = 7                  # day folders read back from the mission's start: a resumed session stays in its first day
AFTER_END      = 5.0                # seconds a run's words are still read after it ended: its last words come just after
ITEMS          = {'reasoning': ('thought', 'summary'), 'message': ('said', 'content')}   # type -> (kind, field of the parts)


def _time(stamp) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _title(text: str) -> tuple[str, str]:
    """Codex starts a reasoning summary with a bold title: '**Planning the ride**\\n\\nI will ...'"""
    if text.startswith('**'):
        end = text.find('**', 2)

        if 2 < end <= 120:
            return text[2:end].strip(), text[end + 2:].strip()

    return '', text


def _item(payload: dict) -> tuple[str, str] | None:
    """(kind, text) of an item the feed shows, else None. The same item comes again as an event (agent_reasoning,
    agent_message, item_completed) under another type: only these two types are read, so it comes once"""
    kind, field = ITEMS.get(payload.get('type'), (None, None))

    if kind is None or (kind == 'said' and payload.get('role') != 'assistant'):
        return None

    parts = [part.get('text') for part in payload.get(field) or [] if isinstance(part, dict)]
    return kind, '\n\n'.join(part for part in parts if isinstance(part, str) and part)


def _who(line: bytes, folders: set[str]) -> str | None:
    """'agent', or a worker's nickname, for a session that started in one of the agent's folders; else None"""
    try:
        record = json.loads(line)
        meta   = record['payload']
        cwd    = str(Path(meta['cwd']).resolve())
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        return None

    if record.get('type') != 'session_meta' or cwd not in folders:
        return None

    source = meta.get('source') if isinstance(meta.get('source'), dict) else {}
    return str(meta.get('agent_nickname') or source.get('agent_nickname') or 'agent')


class RunWords:
    """The words of the last run: from its start, and for AFTER_END after its end. The page and the recorder both
    use it. A new run takes the words over; the run before it then loses its tail"""

    def __init__(self, log: 'SessionLog', clock=time.monotonic):
        self.log, self.clock = log, clock
        self._run   = None                       # (key, began) of the last run
        self._ended = None                       # when this saw it end

    def __call__(self, key, began: float, open_: bool) -> list[dict]:
        if (key, began) != self._run:
            self._run, self._ended = (key, began), None

        if not open_:
            if self._ended is None:
                self._ended = self.clock()

            if self.clock() - self._ended > AFTER_END:
                return []

        return self.log.new(began)


class SessionLog:
    """The new items of the agent's sessions, each once. One thread calls new(); status() may be read from any"""

    def __init__(self, folders, root=CODEX_SESSIONS):
        self.root     = Path(root).expanduser()
        self.folders  = {str(Path(folder).expanduser().resolve()) for folder in folders}
        self._files   = {}                       # path -> {'offset', 'who', 'skip'}; who is None for a session of another folder
        self._texts   = {}                       # (kind, hash of the text) -> None, oldest first: what a forked worker copies
        self._since   = None
        self.sessions = 0                        # the agent's sessions written in the open mission
        self.empty    = 0                        # reasoning items of the open mission with no summary: Codex runs without summaries

    def new(self, since: float) -> list[dict]:
        """The items of the agent's sessions that are not older than since and were not given before, oldest first"""
        if since != self._since:                 # a new mission
            self._since, self.empty = since, 0

        items, sessions = [], 0

        for path, size in self._logs(since):
            items    += self._read(path, size, since)
            sessions += bool(self._files[path]['who'])

        self.sessions = sessions
        return sorted(items, key=lambda item: item['time'])

    def status(self) -> dict:
        return {'sessions': self.sessions, 'empty': self.empty}

    def _logs(self, since: float) -> list[tuple[Path, int]]:
        """(path, size) of the agent's logs written since then, its own sessions first: a worker's copy of their items
        is then known as a copy. Codex files a session under the local day it started"""
        day, last = date.fromtimestamp(since) - timedelta(days=DAYS_BACK), date.today()
        logs      = []

        while day <= last:
            for path in (self.root / f'{day:%Y/%m/%d}').glob('rollout-*.jsonl'):
                file = self._files.setdefault(path, {'offset': 0, 'who': '', 'skip': False})

                if file['who'] is None:
                    continue

                try:
                    stat = path.stat()
                except OSError:
                    continue

                if stat.st_mtime >= since and (file['who'] or self._identify(path, file)):
                    logs.append((path, stat.st_size))

            day += timedelta(days=1)

        return sorted(logs, key=lambda log: (self._files[log[0]]['who'] != 'agent', log[0]))

    def _identify(self, path: Path, file: dict) -> bool:
        """Read the first line, which says whose session it is; False until it says the agent's"""
        try:
            with path.open('rb') as stream:
                line = stream.readline()
        except OSError:
            return False

        if not line.endswith(b'\n'):           # Codex has not ended it yet
            return False

        file['who'], file['offset'] = _who(line, self.folders), len(line)
        return file['who'] is not None

    def _read(self, path: Path, size: int, since: float) -> list[dict]:
        file = self._files[path]

        if size <= file['offset']:
            return []

        try:
            with path.open('rb') as stream:
                stream.seek(file['offset'])
                data = stream.read(MAX_READ)
        except OSError:
            return []

        full = len(data) == MAX_READ

        if file['skip']:                        # the rest of a line longer than MAX_READ (an image, say)
            cut = data.find(b'\n') + 1

            if not cut:
                file['offset'] += len(data)
                return []

            file['offset'] += cut
            file['skip']    = False
            data, full      = data[cut:], False      # the next line starts in this read: not yet known to be long

        end   = data.rfind(b'\n') + 1          # a line Codex has not ended yet is read at the next call
        items = []

        if not end and full:                    # one line fills the read: skip it, or the feed of this session stops
            file['offset'] += len(data)
            file['skip']    = True
            return []

        file['offset'] += end

        for line in data[:end].splitlines():
            if b'"reasoning"' not in line and b'"assistant"' not in line:     # most lines: commands and their output
                continue

            try:
                record = json.loads(line)
            except ValueError:
                continue

            try:
                item = self._new_item(record, since, file['who']) if isinstance(record, dict) else None
            except (TypeError, ValueError, AttributeError):     # one odd item; the next lines still count
                continue

            if item:
                items.append(item)

        return items

    def _new_item(self, record: dict, since: float, who: str) -> dict | None:
        payload = record.get('payload')
        found   = _item(payload) if isinstance(payload, dict) else None
        when    = _time(record.get('timestamp'))

        if not found or when is None or when < since:
            return None

        kind, text = found

        if not text:
            self.empty += kind == 'thought'
            return None

        # each line is read once (the offset), so the same words said again are a new item; only a worker's copy of
        # what its parent said is dropped
        copy = (kind, hash(text))

        if who != 'agent' and copy in self._texts:
            return None

        self._texts[copy] = None

        if len(self._texts) > SEEN_KEEP:
            del self._texts[next(iter(self._texts))]

        title, text = _title(text) if kind == 'thought' else ('', text)
        return {'kind': kind, 'time': when, 'who': who, 'title': title, 'text': text[:MAX_TEXT]}
