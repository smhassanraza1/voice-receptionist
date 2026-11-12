"""The call-length cap, the caller-away check, and the end-of-call usage event."""

import asyncio
import logging
from types import SimpleNamespace

from livekit.agents.voice.events import CloseEvent, CloseReason, UserStateChangedEvent

from receptionist.agent.events import CallRecord, EventPublisher
from receptionist.agent.limits import _CHECK_IN, _GONE, _TIME_UP, CallLimits
from receptionist.agent.settings import AgentSettings
from receptionist.agent.state import CallState


class FakeSession:
    def __init__(self) -> None:
        self.userdata = CallState()
        self.handlers: dict[str, list] = {}
        self.replies: list[dict] = []
        self.closed = False
        self.usage = SimpleNamespace(model_usage=[])

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, ev: object) -> None:
        for handler in self.handlers.get(event, []):
            handler(ev)

    def generate_reply(self, **kwargs: object) -> asyncio.Future:
        self.replies.append(kwargs)
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    async def aclose(self) -> None:
        self.closed = True
        self.emit("close", CloseEvent(reason=CloseReason.USER_INITIATED))


class FakeJob:
    def __init__(self) -> None:
        self.shutdowns: list[str] = []
        self.callbacks: list = []
        self.deleted = 0

    def add_shutdown_callback(self, callback) -> None:
        self.callbacks.append(callback)

    def shutdown(self, reason: str = "") -> None:
        self.shutdowns.append(reason)

    async def delete_room(self) -> None:
        self.deleted += 1


def away(old: str = "listening", new: str = "away") -> UserStateChangedEvent:
    return UserStateChangedEvent(old_state=old, new_state=new)


def limits(max_call_seconds: float = 0, user_away_seconds: float = 0.01):
    session, job = FakeSession(), FakeJob()
    CallLimits(
        job,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        max_call_seconds=max_call_seconds,
        user_away_seconds=user_away_seconds,
    ).attach()
    return session, job


async def test_long_call_says_goodbye_and_hangs_up():
    session, job = limits(max_call_seconds=0.01)
    await asyncio.sleep(0.05)
    assert [r["instructions"] for r in session.replies] == [_TIME_UP]
    assert session.replies[0]["allow_interruptions"] is False
    assert session.closed and job.shutdowns == ["max call duration reached"]
    for callback in job.callbacks:
        await callback()
    assert job.deleted == 1  # the caller is disconnected too


async def test_cap_is_cancelled_when_the_call_ends_first():
    session, job = limits(max_call_seconds=0.02)
    await session.aclose()  # caller hung up, a transfer completed, an operator took over
    await asyncio.sleep(0.05)
    assert session.replies == [] and job.shutdowns == []


async def test_silent_caller_is_asked_then_hung_up_on():
    session, job = limits()
    session.emit("user_state_changed", away())
    assert [r["instructions"] for r in session.replies] == [_CHECK_IN]
    await asyncio.sleep(0.05)
    assert [r["instructions"] for r in session.replies] == [_CHECK_IN, _GONE]
    assert job.shutdowns == ["caller away"]


async def test_caller_who_answers_the_check_in_stays_on_the_line():
    session, job = limits()
    session.emit("user_state_changed", away())
    session.emit("user_state_changed", away("away", "speaking"))
    await asyncio.sleep(0.05)
    assert [r["instructions"] for r in session.replies] == [_CHECK_IN]
    assert job.shutdowns == [] and not session.closed


async def test_caller_waiting_for_staff_is_not_hung_up_on():
    session, job = limits()
    session.userdata.escalated = True
    session.emit("user_state_changed", away())
    await asyncio.sleep(0.05)
    assert session.replies == [] and job.shutdowns == []


def test_max_call_seconds_setting(monkeypatch):
    assert AgentSettings(_env_file=None).max_call_seconds == 600
    monkeypatch.setenv("RECEPTIONIST_MAX_CALL_SECONDS", "0")
    assert AgentSettings(_env_file=None).max_call_seconds == 0


async def test_usage_is_published_when_the_session_closes():
    """The session closes while the room is still connected; shutdown callbacks run later."""
    record = CallRecord(mode="pipeline", models={})
    pub = EventPublisher(None, record)
    sent: list[dict] = []
    pub.publish = sent.append  # type: ignore[method-assign]
    session = FakeSession()
    pub.attach(session)  # type: ignore[arg-type]
    assert not any(e["type"] == "usage" for e in sent)
    await session.aclose()
    assert [e["type"] for e in sent] == ["usage"]
    assert sent[0]["summary"]["turns"] == 0 and record.usage == []


async def test_failed_publish_is_logged(caplog):
    async def publish_data(*_: object, **__: object) -> None:
        raise RuntimeError("engine is closed")

    room = SimpleNamespace(
        isconnected=lambda: True, local_participant=SimpleNamespace(publish_data=publish_data)
    )
    pub = EventPublisher(room, CallRecord(mode="pipeline", models={}))  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="receptionist.agent"):
        pub.handoff("human", "r")
        await asyncio.gather(*pub._tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert "could not publish a call event" in caplog.text
    assert not pub._tasks
