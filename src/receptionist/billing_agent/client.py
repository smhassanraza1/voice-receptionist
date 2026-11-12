"""Client the voice receptionist uses to hand billing questions to the billing agent.

    async with BillingAgentClient(os.environ["BILLING_AGENT_URL"], token) as billing:
        reply = await billing.ask("What do I owe?", patient_id="P-1002", verified=True)
        # speak reply.text; pass reply.context_id on the next turn of the same call,
        # and call billing.close_context(reply.context_id) when the call ends

`ask()` never raises and never blocks past `timeout`: on any failure it returns a speakable
apology with `data["state"] == "unavailable"`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.helpers import get_data_parts, get_text_parts, new_data_part
from a2a.types import (
    CancelTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    TaskState,
)

from receptionist.billing_agent.speech import say

logger = logging.getLogger(__name__)

# Bounds memory if callers hang up mid-question and never send a follow-up.
_MAX_OPEN_TASKS = 1_024


@dataclass(frozen=True, slots=True)
class BillingReply:
    text: str  # speakable reply for the caller
    context_id: str  # pass back on the next turn of the same call
    # The agent's structured data part, plus "state" (completed, input_required, failed,
    # unavailable) and "task_id". See receptionist.billing_agent.executor for the fields.
    data: dict[str, Any] = field(default_factory=dict)


class BillingAgentClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` lets tests route requests to an in-process app (httpx.ASGITransport)."""
        self._base_url = base_url
        self._timeout = timeout
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=timeout, transport=transport
        )
        self._client: Client | None = None
        self._connect_lock = asyncio.Lock()
        # context_id -> id of a task waiting for the caller's answer (input-required)
        self._open_tasks: dict[str, str] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()  # also closes the shared httpx client
        await self._http.aclose()

    async def ask(
        self,
        question: str,
        *,
        patient_id: str | None,
        verified: bool,
        language: str = "en",
        context_id: str | None = None,
    ) -> BillingReply:
        """Ask one billing question; reuse the returned `context_id` for follow-ups."""
        message = Message(
            role=Role.ROLE_USER,
            message_id=uuid.uuid4().hex,
            context_id=context_id or "",
            # Answering a question the agent asked continues that task (A2A input-required).
            task_id=self._open_tasks.pop(context_id, "") if context_id else "",
            parts=[
                Part(text=question),
                new_data_part(
                    {"patient_id": patient_id, "verified": verified, "language": language},
                    media_type="application/json",
                ),
            ],
        )
        try:
            async with asyncio.timeout(self._timeout):
                client = await self._connect()
                events = [
                    event
                    async for event in client.send_message(SendMessageRequest(message=message))
                ]
            return self._to_reply(events[-1], language)
        except Exception as exc:  # the voice agent needs an answer, whatever went wrong
            logger.warning("Billing agent unavailable: %s: %s", type(exc).__name__, exc)
            return BillingReply(
                text=say(language, "unavailable"),
                context_id=context_id or uuid.uuid4().hex,
                data={"state": "unavailable", "error": type(exc).__name__},
            )

    async def close_context(self, context_id: str) -> None:
        """Cancel a task still waiting on the caller, e.g. when the call ends. Best effort."""
        task_id = self._open_tasks.pop(context_id, None)
        if task_id is None or self._client is None:
            return
        try:
            async with asyncio.timeout(self._timeout):
                await self._client.cancel_task(CancelTaskRequest(id=task_id))
        except Exception as exc:
            logger.debug("Could not cancel billing task %s: %s", task_id, exc)

    async def _connect(self) -> Client:
        """Resolve the agent card once and build an SDK client on our authenticated httpx client."""
        async with self._connect_lock:
            if self._client is None:
                card = await A2ACardResolver(self._http, self._base_url).get_agent_card()
                config = ClientConfig(httpx_client=self._http, streaming=False)
                self._client = ClientFactory(config).create(card)
            return self._client

    def _to_reply(self, event: StreamResponse, language: str) -> BillingReply:
        if event.HasField("message"):  # agents may answer with a bare message instead of a task
            message = event.message
            return _build_reply(message.parts, message.context_id, {"state": "completed"}, language)

        task = event.task
        if task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            self._open_tasks[task.context_id] = task.id
            while len(self._open_tasks) > _MAX_OPEN_TASKS:
                self._open_tasks.pop(next(iter(self._open_tasks)))
        state = TaskState.Name(task.status.state).removeprefix("TASK_STATE_").lower()
        # A finished answer is the artifact; a question or an error is the status message.
        parts = task.artifacts[-1].parts if task.artifacts else task.status.message.parts
        return _build_reply(parts, task.context_id, {"state": state, "task_id": task.id}, language)


def _build_reply(
    parts: Sequence[Part], context_id: str, meta: dict[str, str], language: str
) -> BillingReply:
    data = next((item for item in get_data_parts(parts) if isinstance(item, dict)), {})
    text = " ".join(get_text_parts(parts)).strip()
    return BillingReply(
        text=text or say(language, "unavailable"),
        context_id=context_id,
        data={**data, **meta},
    )
