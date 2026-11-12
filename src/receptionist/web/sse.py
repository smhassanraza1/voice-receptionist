"""Server-sent events for the operator console's live escalation list."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from .escalations import EscalationStore

KEEPALIVE_SECONDS = 15.0
RECONNECT_MS = 3000


def format_event(event: str, data: object) -> str:
    """One SSE frame. JSON is emitted on a single line, so a single `data:` field suffices."""
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def escalation_events(
    store: EscalationStore, *, keepalive: float = KEEPALIVE_SECONDS
) -> AsyncIterator[str]:
    """`snapshot` with every escalation, then one `escalation` frame per change.

    Subscribes before taking the snapshot so no change can fall between the two.
    """
    async with store.subscribe() as subscription:
        yield f"retry: {RECONNECT_MS}\n\n"
        snapshot = [e.model_dump(mode="json") for e in store.list()]
        yield format_event("snapshot", {"escalations": snapshot})
        while True:
            try:
                async with asyncio.timeout(keepalive):
                    event = await subscription.get()
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                return
            yield format_event("escalation", event.to_json())
