"""In-memory escalation queue with change notifications for the operator console."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .models import Escalation, EscalationCreate

logger = logging.getLogger(__name__)

EscalationAction = Literal["created", "updated", "resolved"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class EscalationEvent:
    action: EscalationAction
    escalation: Escalation

    def to_json(self) -> dict[str, object]:
        return {"action": self.action, "escalation": self.escalation.model_dump(mode="json")}


class Subscription:
    """One listener's queue. `None` in the queue means the stream must end."""

    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[EscalationEvent | None] = asyncio.Queue(maxsize=maxsize)
        self.closed = False

    def push(self, event: EscalationEvent | None) -> bool:
        if self.closed:
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # A consumer this far behind gets cut off; it reconnects and receives a snapshot.
            self.closed = True
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._queue.put_nowait(None)
            return False
        if event is None:
            self.closed = True
        return True

    async def get(self) -> EscalationEvent | None:
        """Next event, or None once the subscription is closed."""
        return await self._queue.get()


class EscalationStore:
    """Escalations for the current process lifetime.

    All methods must be called from the event loop thread (the web endpoints are async).
    Restarting the server clears the queue, which is acceptable for a demo console.
    """

    def __init__(
        self,
        *,
        max_items: int = 500,
        queue_size: int = 256,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._items: dict[str, Escalation] = {}
        self._subscribers: set[Subscription] = set()
        self._max_items = max_items
        self._queue_size = queue_size
        self._clock = clock
        self._closed = False

    # -- queries ---------------------------------------------------------------------------

    def list(self, status: Literal["open", "resolved"] | None = None) -> list[Escalation]:
        items = [e for e in self._items.values() if status is None or e.status == status]
        return sorted(items, key=lambda e: e.created_at, reverse=True)

    def find_open(self, room: str) -> Escalation | None:
        return next(
            (e for e in self._items.values() if e.room == room and e.status == "open"), None
        )

    # -- mutations -------------------------------------------------------------------------

    def submit(self, request: EscalationCreate) -> tuple[Escalation, bool]:
        """Open an escalation for a room, or refresh the open one. Returns (item, created)."""
        now = self._clock()
        existing = self.find_open(request.room)
        if existing is not None:
            updated = existing.model_copy(
                update={"reason": request.reason, "summary": request.summary, "updated_at": now}
            )
            self._items[updated.id] = updated
            self._publish(EscalationEvent("updated", updated))
            return updated, False

        item = Escalation(
            id=f"esc_{secrets.token_hex(6)}",
            room=request.room,
            reason=request.reason,
            summary=request.summary,
            created_at=now,
            status="open",
        )
        self._items[item.id] = item
        self._evict()
        logger.info("escalation %s opened for room %s", item.id, item.room)
        self._publish(EscalationEvent("created", item))
        return item, True

    def resolve(self, escalation_id: str) -> Escalation | None:
        item = self._items.get(escalation_id)
        if item is None or item.status == "resolved":
            return item
        now = self._clock()
        resolved = item.model_copy(
            update={"status": "resolved", "resolved_at": now, "updated_at": now}
        )
        self._items[resolved.id] = resolved
        logger.info("escalation %s resolved", resolved.id)
        self._publish(EscalationEvent("resolved", resolved))
        return resolved

    def _evict(self) -> None:
        """Keep memory bounded: drop the oldest resolved items first, then the oldest overall."""
        overflow = len(self._items) - self._max_items
        if overflow <= 0:
            return
        by_age = sorted(self._items.values(), key=lambda e: (e.status == "open", e.created_at))
        for item in by_age[:overflow]:
            del self._items[item.id]

    # -- notifications ---------------------------------------------------------------------

    def _publish(self, event: EscalationEvent) -> None:
        for sub in list(self._subscribers):
            if not sub.push(event):
                self._subscribers.discard(sub)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[Subscription]:
        sub = Subscription(self._queue_size)
        if self._closed:
            sub.push(None)
        else:
            self._subscribers.add(sub)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def close(self) -> None:
        """End every open stream and refuse new ones (called from lifespan shutdown).

        uvicorn sends lifespan shutdown only after its graceful-shutdown wait, so open SSE streams
        still hold up exit for up to `timeout_graceful_shutdown` seconds before being cancelled.
        """
        self._closed = True
        for sub in list(self._subscribers):
            sub.push(None)
        self._subscribers.clear()
