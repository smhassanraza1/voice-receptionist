"""A2A `AgentExecutor` that turns a receptionist message into a billing reply.

Task lifecycle per request: submitted (new tasks only) -> working -> one of
  * completed      - the reply is the `billing_reply` artifact (text part + data part)
  * input-required - the reply asks the caller something; it is the status message, and the
                     receptionist answers by sending its next message with the same task id
  * failed         - an unexpected error; the status message is a speakable apology
"""

from __future__ import annotations

import logging
from typing import Any

from a2a.helpers import get_data_parts, get_message_text, new_data_part, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, TaskState
from pydantic import ValidationError

from receptionist.billing_agent.engine import AgentReply, BillingRequest, Caller, ReasoningEngine
from receptionist.billing_agent.security import redact_card_numbers
from receptionist.billing_agent.sessions import SessionStore
from receptionist.billing_agent.speech import say

logger = logging.getLogger(__name__)

REPLY_ARTIFACT = "billing_reply"


def parse_request(message: Message) -> BillingRequest:
    """Read the caller question and identity from an A2A message.

    Card numbers are masked *in place*, before the SDK persists the message to task history,
    so they never reach the task store, the logs or a model. A malformed data part fails
    closed: the caller is treated as unverified.
    """
    card_number_detected = False
    for part in message.parts:
        if part.HasField("text"):
            part.text, found = redact_card_numbers(part.text)
            card_number_detected |= found

    data = next((item for item in get_data_parts(message.parts) if isinstance(item, dict)), {})
    try:
        caller = Caller.model_validate(data)
    except ValidationError:
        logger.warning("Ignoring malformed caller data part; treating caller as unverified")
        caller = Caller()
    return BillingRequest(
        question=get_message_text(message, delimiter=" ").strip(),
        caller=caller,
        card_number_detected=card_number_detected,
    )


class BillingAgentExecutor(AgentExecutor):
    def __init__(self, engine: ReasoningEngine, sessions: SessionStore) -> None:
        self._engine = engine
        self._sessions = sessions

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        task_id, context_id = context.task_id, context.context_id
        if message is None or not task_id or not context_id:
            raise ValueError("execute() needs a message with task and context ids")

        request = parse_request(message)
        updater = TaskUpdater(event_queue, task_id, context_id)
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED, history=[message])
            )
        await updater.start_work()

        try:
            session = self._sessions.get(context_id, request.caller.account_key)
            async with session.lock:
                reply = await self._engine.respond(request, session)
        except Exception:
            logger.exception("Billing request failed (task %s)", task_id)
            apology = say(request.caller.language, "error")
            await updater.failed(updater.new_agent_message([Part(text=apology)]))
            return

        parts = [
            Part(text=reply.text),
            new_data_part(_reply_data(reply, request), media_type="application/json"),
        ]
        if reply.needs_input:
            await updater.requires_input(updater.new_agent_message(parts))
        else:
            await updater.add_artifact(parts, name=REPLY_ARTIFACT, last_chunk=True)
            await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The SDK has already cancelled the running execute(); only the new state is left.
        updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
        await updater.cancel()


def _reply_data(reply: AgentReply, request: BillingRequest) -> dict[str, Any]:
    return {
        "intent": reply.intent,
        "engine": reply.engine,
        "language": reply.language,
        "verified": request.caller.can_access_account,
        "needs_verification": reply.needs_verification,
        "card_number_redacted": request.card_number_detected,
        "facts": reply.facts,
    }
