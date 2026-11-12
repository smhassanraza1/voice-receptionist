"""A2A task store that forgets tasks, the way `SessionStore` forgets conversations.

A task holds the caller's questions and the billing replies, and the SDK's `InMemoryTaskStore`
keeps every one for the life of the process. This store drops a task once it has gone
`ttl_s` without being saved, and drops the least recently saved tasks beyond `max_tasks`.

`tasks/list` is refused. Every request authenticates with the one shared token, so every task
has the same owner and a listing would give any holder of the token every call's history. The
receptionist only reads a task by its id, to continue it.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable

from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task
from a2a.utils.errors import UnsupportedOperationError


class ExpiringTaskStore(TaskStore):
    """TTL + size-bounded `InMemoryTaskStore`. Runs on the A2A server's single event loop."""

    def __init__(
        self, *, ttl_s: float, max_tasks: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._store = InMemoryTaskStore()
        self._ttl_s = ttl_s
        self._max_tasks = max_tasks
        self._clock = clock
        # (owner, task id) -> (last saved, a context resolving to that owner), oldest first.
        # Only the user is kept: the request's context also carries its headers and token.
        self._saved: OrderedDict[tuple[str, str], tuple[float, ServerCallContext]] = OrderedDict()

    async def save(self, task: Task, context: ServerCallContext) -> None:
        now = self._clock()
        await self._evict_expired(now)
        await self._store.save(task, context)
        key = (resolve_user_scope(context), task.id)
        self._saved[key] = (now, ServerCallContext(user=context.user))
        self._saved.move_to_end(key)
        while len(self._saved) > self._max_tasks:
            (_, task_id), (_, owner) = self._saved.popitem(last=False)
            await self._store.delete(task_id, owner)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        await self._evict_expired(self._clock())
        return await self._store.get(task_id, context)

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        raise UnsupportedOperationError(message="Listing tasks is not supported.")

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        self._saved.pop((resolve_user_scope(context), task_id), None)
        await self._store.delete(task_id, context)

    async def _evict_expired(self, now: float) -> None:
        # Least recently saved first, so stop at the first live task.
        while self._saved:
            (_, task_id), (saved_at, owner) = next(iter(self._saved.items()))
            if now - saved_at < self._ttl_s:
                return
            self._saved.popitem(last=False)
            await self._store.delete(task_id, owner)
