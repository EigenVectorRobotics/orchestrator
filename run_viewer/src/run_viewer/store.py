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
