"""Retention of A2A tasks, which carry the caller's questions and the billing replies."""

from __future__ import annotations

import pytest
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.types import ListTasksRequest, Task, TaskState, TaskStatus
from a2a.utils.errors import UnsupportedOperationError

from receptionist.billing_agent.task_store import ExpiringTaskStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Receptionist(User):
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return "voice-receptionist"


CONTEXT = ServerCallContext(user=Receptionist(), state={"headers": {"authorization": "Bearer x"}})


def task(task_id: str) -> Task:
    return Task(
        id=task_id, context_id="call-1", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED)
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


async def test_tasks_expire_after_the_ttl(clock: FakeClock) -> None:
    store = ExpiringTaskStore(ttl_s=60, max_tasks=10, clock=clock)
    await store.save(task("a"), CONTEXT)
    clock.now += 59
    await store.save(task("b"), CONTEXT)
    assert await store.get("a", CONTEXT) is not None
    clock.now += 2
    assert await store.get("a", CONTEXT) is None
    assert await store.get("b", CONTEXT) is not None


async def test_saving_a_task_again_keeps_it(clock: FakeClock) -> None:
    store = ExpiringTaskStore(ttl_s=60, max_tasks=10, clock=clock)
    await store.save(task("a"), CONTEXT)
    clock.now += 50
    await store.save(task("a"), CONTEXT)  # e.g. an input-required task answered later
    clock.now += 50
    assert await store.get("a", CONTEXT) is not None


async def test_the_least_recently_saved_tasks_go_beyond_max_tasks(clock: FakeClock) -> None:
    store = ExpiringTaskStore(ttl_s=60, max_tasks=2, clock=clock)
    for task_id in ("a", "b", "c"):
        await store.save(task(task_id), CONTEXT)
    assert [await store.get(t, CONTEXT) is not None for t in ("a", "b", "c")] == [
        False,
        True,
        True,
    ]


async def test_tasks_cannot_be_listed(clock: FakeClock) -> None:
    store = ExpiringTaskStore(ttl_s=60, max_tasks=2, clock=clock)
    await store.save(task("a"), CONTEXT)
    with pytest.raises(UnsupportedOperationError):
        await store.list(ListTasksRequest(), CONTEXT)
