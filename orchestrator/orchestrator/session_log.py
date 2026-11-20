"""The task agent's own words, read from its session log, for the Mission feed.

The task agent is a Codex or a Claude Code session that starts in the agent's folder (agent or agent_discovery).
This module reads the logs of both tools:

  - Codex writes each session to ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl: a session_meta line (with the cwd)
    and then one line per item.
  - Claude Code writes each session to ~/.claude/projects/<cwd, each other character a ->/<id>.jsonl, and a
    worker's (subagent's) to <id>/subagents/agent-*.jsonl, with its type in agent-*.meta.json. Each line holds one
    part of a message.

It reads two kinds of item, and nothing else:

  - thought: the summary of the model's reasoning. Codex writes it only when it starts with
    -c model_reasoning_summary="detailed", Claude Code only with showThinkingSummaries; else the text is empty.
  - said:    what the agent says (role assistant).

It does not read commands, their output or the prompts: these can hold file contents, and the page shows only what
the agent thinks and says. No ROS here: the node calls new() about once a second while a mission is open.
"""

import re
import json
import time

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable


CODEX_SESSIONS = Path.home() / '.codex' / 'sessions'
CLAUDE_LOGS    = Path.home() / '.claude' / 'projects'
MAX_TEXT       = 2000               # characters of one item; a longer item is cut
MAX_READ       = 4 * 1024 * 1024    # bytes of one file in one call: a long log is read over a few calls
SEEN_KEEP      = 5000               # texts kept to drop a copy (a forked worker's log starts with its parent's items)
DAYS_BACK      = 7                  # day folders read back from the mission's start: a resumed session stays in its first day
LIST_AGAIN_S   = 2.0                # seconds a log folder is listed at each call after it changed
AFTER_END      = 5.0                # seconds a run's words are still read after it ended: its last words come just after
ITEMS          = {'reasoning': ('thought', 'summary'), 'message': ('said', 'content')}   # Codex: type -> (kind, field of the parts)
CLAUDE_PARTS   = {'thinking': 'thought', 'text': 'said'}                                 # Claude Code: type of a part -> kind


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


def _codex_items(record: dict) -> list[tuple[str, str]]:
    """(kind, text) of an item the feed shows. The same item comes again as an event (agent_reasoning,
    agent_message, item_completed) under another type: only these two types are read, so it comes once"""
    payload     = record.get('payload')
    payload     = payload if isinstance(payload, dict) else {}
    kind, field = ITEMS.get(payload.get('type'), (None, None))

    if kind is None or (kind == 'said' and payload.get('role') != 'assistant'):
        return []

    parts = [part.get('text') for part in payload.get(field) or [] if isinstance(part, dict)]
    return [(kind, '\n\n'.join(part for part in parts if isinstance(part, str) and part))]


def _claude_items(record: dict) -> list[tuple[str, str]]:
    """(kind, text) of each thinking and text part of an assistant line; tool calls are not read. Other lines can
    hold an assistant message too (api-request-blob: a copy of an earlier turn), so the type of the line decides"""
    message = record.get('message')

    if record.get('type') != 'assistant' or not isinstance(message, dict):
        return []

    parts = message.get('content') if isinstance(message.get('content'), list) else []
    found = [(CLAUDE_PARTS.get(part.get('type')), part.get(part.get('type'))) for part in parts if isinstance(part, dict)]
    return [(kind, text if isinstance(text, str) else '') for kind, text in found if kind]


PARSERS = {'codex': _codex_items, 'claude': _claude_items}     # the tool of a log -> (kind, text) of its items


def claude_folder(cwd: Path) -> str:
    """The folder name Claude Code gives the logs of a session that starts in cwd"""
    return re.sub(r'[^A-Za-z0-9]', '-', str(cwd))


def _worker(log: Path) -> str:
    """A Claude Code worker's name: its type (Explore, say), from the meta file beside its log"""
    try:
        meta = json.loads(log.with_suffix('.meta.json').read_text())
    except (OSError, ValueError):
        meta = None

    return str(meta.get('agentType') or 'worker') if isinstance(meta, dict) else 'worker'


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

    def __init__(self, folders, root=CODEX_SESSIONS, claude_root=CLAUDE_LOGS):
        self.root     = Path(root).expanduser()
        self.claude   = Path(claude_root).expanduser()
        folders       = [Path(folder).expanduser() for folder in folders]
        self.folders  = {str(folder.resolve()) for folder in folders}
        # Claude Code names the folder of the logs after the cwd as the shell spells it, which may be a link
        self._claude  = sorted({claude_folder(spelling) for folder in folders for spelling in (folder.absolute(), folder.resolve())})
        self._files   = {}                       # path -> {'offset', 'who', 'skip', 'tool'}; who is None for a session of another folder
        self._listed  = {}                       # Claude Code log folder -> (its mtime, its logs): listed again on a change
        self._texts   = {}                       # (kind, hash of the text) -> None, oldest first: what a forked worker copies
        self._since   = None
        self.sessions = 0                        # the agent's sessions written in the open mission
        self.empty    = 0                        # reasoning items of the open mission with no summary: the tool writes none
        self.quiet    = None                     # the tool of the last of them: 'codex' or 'claude'

    def new(self, since: float) -> list[dict]:
        """The items of the agent's sessions that are not older than since and were not given before, oldest first"""
        if since != self._since:                 # a new mission
            self._since, self.empty, self.quiet = since, 0, None

        items, sessions = [], 0

        # the agent's own sessions first: a worker's copy of their items is then known as a copy
        for path, size in sorted(self._logs(since) + self._claude_logs(since),
                                 key=lambda log: (self._files[log[0]]['who'] != 'agent', log[0])):
            items    += self._read(path, size, since)
            sessions += bool(self._files[path]['who'])

        self.sessions = sessions
        return sorted(items, key=lambda item: item['time'])

    def status(self) -> dict:
        return {'sessions': self.sessions, 'empty': self.empty, 'quiet': self.quiet}

    def _logs(self, since: float) -> list[tuple[Path, int]]:
        """(path, size) of the agent's Codex logs written since then. Codex files a session under the local day it
        started"""
        day, last = date.fromtimestamp(since) - timedelta(days=DAYS_BACK), date.today()
        logs      = []

        while day <= last:
            for path in (self.root / f'{day:%Y/%m/%d}').glob('rollout-*.jsonl'):
                file = self._file(path, 'codex', lambda: '')

                if file['who'] is None:
                    continue

                try:
                    stat = path.stat()
                except OSError:
                    continue

                if stat.st_mtime >= since and (file['who'] or self._identify(path, file)):
                    logs.append((path, stat.st_size))

            day += timedelta(days=1)

        return logs

    def _claude_logs(self, since: float) -> list[tuple[Path, int]]:
        """(path, size) of the agent's Claude Code logs written since then, and of their workers. The folder of the
        logs says whose session it is. A worker's log has its own time: its session's log waits while it works"""
        logs, recent = [], since - DAYS_BACK * 86400

        for folder in self._claude:
            for path in self._listing(self.claude / folder, '*.jsonl'):
                main = self._size(path, since)

                if main is None and self._size(path, recent) is None:
                    continue                                    # an old session: its workers ended long ago

                if main is not None:
                    self._file(path, 'claude', lambda: 'agent')
                    logs.append((path, main))

                for worker in self._listing(path.with_suffix('') / 'subagents', 'agent-*.jsonl'):
                    if (size := self._size(worker, since)) is not None:
                        self._file(worker, 'claude', lambda worker=worker: _worker(worker))
                        logs.append((worker, size))

        return logs

    @staticmethod
    def _size(path: Path, since: float) -> int | None:
        """The size of a log written since then, else None"""
        try:
            stat = path.stat()
        except OSError:
            return None

        return stat.st_size if stat.st_mtime >= since else None

    def _listing(self, folder: Path, pattern: str) -> list[Path]:
        """The logs in a folder. Listed again when a new file changes the folder, and while its time is new: a file
        made in the same tick as the last listing does not change it"""
        try:
            mtime = folder.stat().st_mtime
        except OSError:
            return []

        if self._listed.get(folder, (None,))[0] != mtime or time.time() - mtime < LIST_AGAIN_S:
            self._listed[folder] = (mtime, sorted(folder.glob(pattern)))

        return self._listed[folder][1]

    def _file(self, path: Path, tool: str, who: Callable[[], str]) -> dict:
        """What this knows of one log; who is asked once, when the log is new"""
        if path not in self._files:
            self._files[path] = {'offset': 0, 'who': who(), 'skip': False, 'tool': tool}

        return self._files[path]

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
                items += self._new_items(record, since, file) if isinstance(record, dict) else []
            except (TypeError, ValueError, AttributeError):     # one odd item; the next lines still count
                continue

        return items

    def _new_items(self, record: dict, since: float, file: dict) -> list[dict]:
        when, who, items = _time(record.get('timestamp')), file['who'], []

        if when is None or when < since:
            return []

        for kind, text in PARSERS[file['tool']](record):
            if not text:
                if kind == 'thought':
                    self.empty, self.quiet = self.empty + 1, file['tool']

                continue

            # each line is read once (the offset), so the same words said again are a new item; only a worker's copy
            # of what its parent said is dropped
            copy = (kind, hash(text))

            if who != 'agent' and copy in self._texts:
                continue

            self._texts[copy] = None

            if len(self._texts) > SEEN_KEEP:
                del self._texts[next(iter(self._texts))]

            title, text = _title(text) if kind == 'thought' else ('', text)
            items.append({'kind': kind, 'time': when, 'who': who, 'title': title, 'text': text[:MAX_TEXT]})

        return items
