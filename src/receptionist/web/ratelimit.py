"""A small in-memory sliding-window rate limiter (single process, per client key)."""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """Allow at most `limit` events per `window` seconds for each key.

    State lives in this process only, which is fine for a single-instance demo server; a
    multi-instance deployment would back this with Redis or the edge proxy's limiter.
    """

    def __init__(
        self,
        limit: int,
        window: float,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window <= 0:
            raise ValueError("limit must be >= 1 and window > 0")
        self.limit = limit
        self.window = window
        self._max_keys = max_keys
        self._clock = clock
        self._events: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
            while len(self._events) > self._max_keys:
                self._events.popitem(last=False)
        else:
            self._events.move_to_end(key)
        cutoff = now - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def retry_after(self, key: str) -> float | None:
        """Seconds until `key` may act again, or None if it is under the limit."""
        now = self._clock()
        events = self._prune(key, now)
        if len(events) < self.limit:
            return None
        return max(events[0] + self.window - now, 0.0)

    def record(self, key: str) -> None:
        now = self._clock()
        self._prune(key, now).append(now)

    def hit(self, key: str) -> float | None:
        """Record an event unless limited. Returns the retry-after delay when limited."""
        delay = self.retry_after(key)
        if delay is None:
            self.record(key)
        return delay

    def reset(self, key: str) -> None:
        self._events.pop(key, None)
