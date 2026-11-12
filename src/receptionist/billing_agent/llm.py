"""LLM engine: an OpenAI tool-calling loop over the billing tools.

Conversation history lives in the per-context session (not on OpenAI's side), so follow-ups
like "and what about the older one?" resolve against earlier tool results. Any API failure
falls back to the rule-based engine so the caller still gets an answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import openai
from openai import AsyncOpenAI

from receptionist.billing_agent.engine import AgentReply, BillingRequest, Caller, ReasoningEngine
from receptionist.billing_agent.ledger import PLAN_MAX_MONTHS, PLAN_MIN_BALANCE, BillingLedger
from receptionist.billing_agent.sessions import ChatMessage, Session
from receptionist.billing_agent.speech import money
from receptionist.billing_agent.tools import BillingTools
from receptionist.common import clock
from receptionist.common.fixtures import CLINIC

logger = logging.getLogger(__name__)

_LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "hi": "Hindi",
    "ja": "Japanese",
}

_SYSTEM_PROMPT = """\
You are the billing specialist for {clinic}. The clinic's voice receptionist relays a caller's \
billing question to you and reads your reply aloud, word for word. Today is {today}.

Reply in {language}. Use one to three short sentences of plain spoken language: no markdown, \
lists, links or emojis. Say amounts like "$185.40" and dates like "September 1".

Rules:
- Use only facts returned by your tools. Never guess balances, dates or claim outcomes.
- Never ask for, repeat or accept card numbers. Offer to text a secure payment link to the \
phone on file instead.
- Call send_payment_link only after the caller has agreed to receive the link.
- Payment plans are available for balances of {plan_minimum} or more, with up to \
{plan_months} monthly payments.
- {identity_rule}
"""

_VERIFIED_RULE = (
    "The receptionist has verified this caller's identity; you may discuss their account."
)
_UNVERIFIED_RULE = (
    "The caller is NOT verified. Share only general billing information (accepted insurance, "
    "payment options, payment plan policy) and explain that their identity must be verified "
    "before you can discuss their account."
)


class OpenAIEngine:
    name = "llm"

    def __init__(
        self,
        ledger: BillingLedger,
        *,
        client: AsyncOpenAI,
        model: str,
        fallback: ReasoningEngine,
        timeout_s: float,
        reasoning_effort: str | None = None,
        max_tool_rounds: int = 4,
    ) -> None:
        self._ledger = ledger
        self._client = client
        self._model = model
        self._fallback = fallback
        self._timeout_s = timeout_s
        self._reasoning_effort = reasoning_effort
        self._max_tool_rounds = max_tool_rounds

    async def respond(self, request: BillingRequest, session: Session) -> AgentReply:
        if request.card_number_detected:
            # Deterministic refusal; even the redacted text never reaches the model.
            return await self._respond_with_fallback(request, session)

        tools = BillingTools(self._ledger, request.caller)
        try:
            # One budget for the whole tool loop, kept under the receptionist's own timeout.
            async with asyncio.timeout(self._timeout_s):
                text, turn, tools_called = await self._run(request, session, tools)
        except (openai.OpenAIError, ToolLoopError, TimeoutError):
            logger.warning("LLM call failed; answering with the rule-based engine", exc_info=True)
            return await self._respond_with_fallback(request, session)

        session.add_turn(turn)
        return AgentReply(
            text=text,
            intent=tools_called[-1] if tools_called else "general",
            engine=self.name,
            language=request.caller.language,
            # The model signals it is waiting on the caller by asking a question.
            needs_input=text.endswith("?"),
            needs_verification=not request.caller.can_access_account,
            facts={"model": self._model, "tools_called": tools_called},
        )

    async def _respond_with_fallback(self, request: BillingRequest, session: Session) -> AgentReply:
        reply = await self._fallback.respond(request, session)
        # Recorded so later model turns still see the whole conversation.
        session.add_turn(
            [
                {"role": "user", "content": request.question},
                {"role": "assistant", "content": reply.text},
            ]
        )
        return reply

    async def _run(
        self, request: BillingRequest, session: Session, tools: BillingTools
    ) -> tuple[str, list[ChatMessage], list[str]]:
        user_message: ChatMessage = {"role": "user", "content": request.question}
        turn: list[ChatMessage] = [user_message]
        messages = [
            {"role": "system", "content": _system_prompt(request.caller)},
            *session.history(),
            user_message,
        ]
        options: dict[str, Any] = {"tools": tools.specs()}
        if self._reasoning_effort:
            options["reasoning_effort"] = self._reasoning_effort
        tools_called: list[str] = []

        for _ in range(self._max_tool_rounds):
            completion = await self._client.chat.completions.create(
                model=self._model, messages=messages, **options
            )
            message = completion.choices[0].message
            calls = [call for call in message.tool_calls or [] if call.type == "function"]
            if not calls:
                text = (message.content or "").strip()
                if not text:
                    raise ToolLoopError("The model returned an empty reply.")
                turn.append({"role": "assistant", "content": text})
                return text, turn, tools_called

            new_messages: list[ChatMessage] = [
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in calls
                    ],
                }
            ]
            for call in calls:
                tools_called.append(call.function.name)
                result = tools.call(call.function.name)
                new_messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )
            messages.extend(new_messages)
            turn.extend(new_messages)

        raise ToolLoopError(f"No final answer after {self._max_tool_rounds} tool rounds.")


class ToolLoopError(RuntimeError):
    """The model did not produce a usable final answer."""


def _system_prompt(caller: Caller) -> str:
    return _SYSTEM_PROMPT.format(
        clinic=CLINIC["name"],
        today=clock.today_str(),
        language=_LANGUAGE_NAMES.get(caller.language, caller.language),
        plan_minimum=money(PLAN_MIN_BALANCE),
        plan_months=PLAN_MAX_MONTHS,
        identity_rule=_VERIFIED_RULE if caller.can_access_account else _UNVERIFIED_RULE,
    )
