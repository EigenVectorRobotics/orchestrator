"""Thread-safe list of viewer events that clients can wait on."""

import threading


class EventStore:
    """Keep every event and wake waiting clients."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[dict] = []
        self.latest: dict[str, dict] = {}       # state that only its last value matters for, e.g. nav: polled, not streamed
        self.status = ''
        self.cond = threading.Condition()

    def add(
        self,
        event: dict,
    ) -> None:
        """Add an event and wake clients."""
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def set_latest(
        self,
        name: str,
        item: dict,
    ) -> None:
        """Keep only the last value of a state; it is not added to the event list."""
        with self.cond:
            self.latest[name] = item

    def set_status(
        self,
        text: str,
    ) -> None:
        """Set the one-line source status and wake clients."""
        with self.cond:
            if self.status == text:
                return
            self.status = text
            self.add({'statusline': text})      # the condition's lock is an RLock: add may take it again

    def since(
        self,
        seq: int,
        timeout_s: float,
    ) -> list[dict]:
        """Return events from index seq on, waiting up to timeout_s when there are none."""
        with self.cond:
            if len(self.events) <= seq:
                self.cond.wait(timeout_s)
            return self.events[seq:]

    def snapshot(self) -> tuple[list[dict], int]:
        """The current session's events with repeated node results folded, and the index to go on from."""
        with self.cond:
            events = self.events[:]
        start = max((i for i, e in enumerate(events) if e.get('kind') == 'session'), default=0)
        status = [e for e in events[:start] if 'statusline' in e][-1:]
        return status + fold(events[start:]), len(events)


def fold(
    events: list[dict],
) -> list[dict]:
    """Merge a node result into the last event of the same node when the status repeats; it keeps the newest time and a count."""
    out, last = [], {}
    for event in events:
        if event.get('kind') == 'tree':
            last = {}
        if event.get('kind') != 'node' or not event.get('path'):
            out.append(event)
            continue
        prev = last.get(event['path'])
        if prev is not None and event.get('status') != 'running' and prev.get('status') == event.get('status'):
            prev['time'], prev['repeat'] = event['time'], prev.get('repeat', 1) + 1
            continue
        last[event['path']] = dict(event)
        out.append(last[event['path']])
    return out
