"""The operator console's live escalation stream (server-sent events)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import httpx

from receptionist.web.escalations import EscalationStore
from receptionist.web.models import EscalationCreate
from receptionist.web.sse import escalation_events, format_event

INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def parse_frame(frame: str) -> tuple[str | None, object]:
    event: str | None = None
    data: list[str] = []
    for line in frame.strip("\n").split("\n"):
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data.append(line.removeprefix("data: "))
    return event, json.loads("\n".join(data)) if data else None


def request(room: str = "call-000000000001") -> EscalationCreate:
    return EscalationCreate(room=room, reason="Needs a human", summary="")


async def test_stream_starts_with_snapshot_then_streams_changes() -> None:
    store = EscalationStore()
    existing, _ = store.submit(request("call-000000000001"))
    stream: AsyncIterator[str] = escalation_events(store, keepalive=5)

    assert (await anext(stream)).startswith("retry: ")
    event, data = parse_frame(await anext(stream))
    assert event == "snapshot"
    assert data == {"escalations": [existing.model_dump(mode="json")]}

    created, _ = store.submit(request("call-000000000002"))
    event, data = parse_frame(await anext(stream))
    assert event == "escalation"
    assert data == {"action": "created", "escalation": created.model_dump(mode="json")}

    store.resolve(created.id)
    _, data = parse_frame(await anext(stream))
    assert isinstance(data, dict)
    assert data["action"] == "resolved"
    assert data["escalation"]["status"] == "resolved"

    # Shutdown ends open streams instead of leaving uvicorn waiting on them.
    store.close()
    assert [frame async for frame in stream] == []
    assert store.subscriber_count == 0


async def test_stream_sends_keepalives_while_idle() -> None:
    store = EscalationStore()
    stream = escalation_events(store, keepalive=0.01)
    await anext(stream)  # retry
    await anext(stream)  # snapshot
    assert await anext(stream) == ": keepalive\n\n"
    await stream.aclose()
    assert store.subscriber_count == 0


async def test_slow_subscriber_is_cut_off_not_buffered_forever() -> None:
    store = EscalationStore(queue_size=2)
    stream = escalation_events(store, keepalive=5)
    await anext(stream)
    await anext(stream)
    for i in range(5):
        store.submit(request(f"call-00000000001{i}"))
    assert store.subscriber_count == 0
    frames = [frame async for frame in stream]
    assert len(frames) <= 2  # whatever was queued, then the stream ends (client re-snapshots)


def test_format_event_is_one_data_line() -> None:
    frame = format_event("escalation", {"summary": "line one\nline two"})
    assert frame.count("\n") == 3
    assert frame.endswith("\n\n")


def iter_frames(lines: Iterator[str]) -> Iterator[str]:
    buffer: list[str] = []
    for line in lines:
        if line == "":
            if buffer:
                yield "\n".join(buffer) + "\n\n"
                buffer = []
            continue
        buffer.append(line)


def test_stream_over_real_http(
    live_server: str, internal_headers: dict[str, str], operator_headers: dict[str, str]
) -> None:
    """End to end through uvicorn: headers, auth, snapshot, then a pushed change."""
    with httpx.Client(base_url=live_server, timeout=5) as http:
        assert http.get("/api/escalations/stream").status_code == 401

        with http.stream("GET", "/api/escalations/stream", headers=operator_headers) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["cache-control"].startswith("no-cache")
            frames = iter_frames(resp.iter_lines())

            assert next(frames).startswith("retry:")
            event, data = parse_frame(next(frames))
            assert event == "snapshot"
            assert data == {"escalations": []}

            posted = http.post(
                "/api/escalations",
                json={"room": "call-abcdefabcdef", "reason": "Wants a person", "summary": "…"},
                headers=internal_headers,
            )
            assert posted.status_code == 201

            event, data = parse_frame(next(frames))
            assert event == "escalation"
            assert isinstance(data, dict)
            assert data["action"] == "created"
            assert data["escalation"]["id"] == posted.json()["id"]
