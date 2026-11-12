"""The OpenAI tool-calling loop, driven by a scripted fake client (no network, no API key)."""

from __future__ import annotations

import json
from typing import Any

import openai
import pytest
from openai.types.chat import ChatCompletion

from receptionist.billing_agent.config import BillingAgentSettings
from receptionist.billing_agent.engine import BillingRequest, Caller
from receptionist.billing_agent.ledger import BillingLedger
from receptionist.billing_agent.llm import OpenAIEngine
from receptionist.billing_agent.rules import RuleBasedEngine
from receptionist.billing_agent.server import build_engine
from receptionist.billing_agent.sessions import Session

from ._support import TOKEN

VERIFIED = Caller(patient_id="P-1002", verified=True)


def _completion(content: str | None = None, *tools: str) -> ChatCompletion:
    tool_calls = [
        {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for n, name in enumerate(tools)
    ]
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "fake",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if tools else "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls or None,
                    },
                }
            ],
        }
    )


class FakeOpenAI:
    """Stands in for AsyncOpenAI: replays scripted completions and records each request."""

    def __init__(self, *script: ChatCompletion | Exception) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    async def create(self, **request: Any) -> ChatCompletion:
        self.requests.append(json.loads(json.dumps(request)))  # snapshot, not live lists
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def ledger() -> BillingLedger:
    return BillingLedger()


def _engine(ledger: BillingLedger, client: FakeOpenAI) -> OpenAIEngine:
    return OpenAIEngine(
        ledger,
        client=client,  # type: ignore[arg-type]
        model="gpt-test",
        fallback=RuleBasedEngine(ledger),
        timeout_s=5,
    )


def _session() -> Session:
    return Session(account_key=VERIFIED.account_key, max_turns=8)


async def test_tool_loop_answers_from_tool_results(ledger: BillingLedger) -> None:
    client = FakeOpenAI(
        _completion(None, "get_account_summary"),
        _completion("Your balance is $185.40, for your echocardiogram on September 1."),
    )

    reply = await _engine(ledger, client).respond(
        BillingRequest("What do I owe?", VERIFIED), _session()
    )

    assert reply.text.startswith("Your balance is $185.40")
    assert reply.engine == "llm"
    assert reply.facts["tools_called"] == ["get_account_summary"]
    tool_result = json.loads(client.requests[1]["messages"][-1]["content"])
    assert tool_result["balance_due"] == 185.40


async def test_follow_up_turns_see_earlier_tool_results(ledger: BillingLedger) -> None:
    session = _session()
    client = FakeOpenAI(
        _completion(None, "get_account_summary"),
        _completion("Your echocardiogram has $185.40 left."),
        _completion("The older one, your follow-up visit, is paid in full."),
    )
    engine = _engine(ledger, client)

    await engine.respond(BillingRequest("What's my balance?", VERIFIED), session)
    await engine.respond(BillingRequest("And what about the older one?", VERIFIED), session)

    follow_up = client.requests[-1]["messages"]
    roles = [message["role"] for message in follow_up]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert "INV-5003" in follow_up[3]["content"]


async def test_unverified_callers_are_not_offered_account_tools(ledger: BillingLedger) -> None:
    client = FakeOpenAI(_completion("I'll need to verify your identity first."))

    reply = await _engine(ledger, client).respond(
        BillingRequest("What's my balance?", Caller(patient_id="P-1002", verified=False)),
        Session(account_key=None, max_turns=8),
    )

    tool_names = [tool["function"]["name"] for tool in client.requests[0]["tools"]]
    assert tool_names == ["list_accepted_insurance"]
    assert "NOT verified" in client.requests[0]["messages"][0]["content"]
    assert reply.needs_verification is True


async def test_api_errors_fall_back_to_the_rule_engine(ledger: BillingLedger) -> None:
    client = FakeOpenAI(openai.OpenAIError("service unavailable"))
    session = _session()

    reply = await _engine(ledger, client).respond(
        BillingRequest("What's my balance?", VERIFIED), session
    )

    assert reply.engine == "rules"
    assert "$185.40" in reply.text
    assert [m["role"] for m in session.history()] == ["user", "assistant"]


async def test_card_numbers_never_reach_the_model(ledger: BillingLedger) -> None:
    client = FakeOpenAI()

    reply = await _engine(ledger, client).respond(
        BillingRequest("my card is **** ****", VERIFIED, card_number_detected=True), _session()
    )

    assert client.requests == []
    assert reply.intent == "card_number"


@pytest.mark.parametrize(
    ("overrides", "engine_type"),
    [
        ({}, RuleBasedEngine),  # no OPENAI_API_KEY
        ({"OPENAI_API_KEY": ""}, RuleBasedEngine),  # left blank in .env
        ({"OPENAI_API_KEY": "sk-test", "llm": "off"}, RuleBasedEngine),
        ({"OPENAI_API_KEY": "sk-test", "llm": "auto"}, OpenAIEngine),
    ],
)
def test_engine_selection(ledger: BillingLedger, overrides: dict, engine_type: type) -> None:
    settings = BillingAgentSettings(token=TOKEN, _env_file=None, **overrides)

    assert isinstance(build_engine(settings, ledger), engine_type)
