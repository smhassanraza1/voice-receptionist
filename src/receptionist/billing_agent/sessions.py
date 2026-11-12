"""Per-`context_id` conversation state, bounded in size and age."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ChatMessage = dict[str, Any]


@dataclass
class Session:
    """State for one A2A context (one phone call)."""

    account_key: str | None
    max_turns: int
    # The last reply offered to text a payment link; a bare "yes" next turn accepts it.
    payment_link_offered: bool = False
    # LLM mode: past turns as OpenAI chat messages. Each turn is kept whole so a tool call is
    # never separated from its result when old turns are dropped.
    turns: list[list[ChatMessage]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    last_seen: float = 0.0

    def history(self) -> list[ChatMessage]:
        return [message for turn in self.turns for message in turn]

    def add_turn(self, messages: list[ChatMessage]) -> None:
        self.turns.append(messages)
        del self.turns[: -self.max_turns]


class SessionStore:
    """LRU + TTL store. Not thread-safe; the A2A server runs on a single event loop."""

    def __init__(
        self,
        *,
        ttl_s: float,
        max_sessions: int,
        max_turns: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_sessions = max_sessions
        self._max_turns = max_turns
        self._clock = clock
        self._sessions: OrderedDict[str, Session] = OrderedDict()

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, context_id: str, account_key: str | None) -> Session:
        """The session for `context_id`, started fresh if it expired or the account changed."""
        now = self._clock()
        self._evict_expired(now)
        session = self._sessions.get(context_id)
        if session is None or session.account_key != account_key:
            session = Session(account_key=account_key, max_turns=self._max_turns)
            self._sessions[context_id] = session
        self._sessions.move_to_end(context_id)
        session.last_seen = now
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return session

    def _evict_expired(self, now: float) -> None:
        # Least recently used first, so stop at the first live session.
        while self._sessions:
            oldest = next(iter(self._sessions.values()))
            if now - oldest.last_seen < self._ttl_s:
                return
            self._sessions.popitem(last=False)
