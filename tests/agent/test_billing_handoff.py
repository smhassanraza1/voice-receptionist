"""Receptionist -> billing liaison handoff, answered by the real A2A billing agent in-process."""

import httpx
import pytest
from fakes import Reply, ScriptedLLM, last_item, last_user_text
from livekit.agents import AgentSession, llm
from livekit.agents.testing import fake_job_context

from receptionist.agent.agents import BillingLiaisonAgent, ReceptionistAgent
from receptionist.agent.events import CallRecord, EventPublisher
from receptionist.agent.handoff import HumanHandoff
from receptionist.agent.settings import AgentSettings
from receptionist.agent.state import CallState
from receptionist.billing_agent.client import BillingAgentClient
from receptionist.billing_agent.config import BillingAgentSettings
from receptionist.billing_agent.server import build_app

TOKEN = "billing-test-token-123"


def names(tools) -> set[str]:
    return {t.info.name for t in tools if hasattr(t, "info")}


def script(ctx: llm.ChatContext, tools) -> Reply:
    item = last_item(ctx)
    if item.type == "function_call_output":
        return Reply(item.output if item.name == "ask_billing_department" else "One moment.")
    if "ask_billing_department" in names(tools):
        question = last_user_text(ctx) or "What is my balance?"
        return Reply(tool_calls=[("ask_billing_department", {"question": question})])
    if "balance" in last_user_text(ctx).lower():
        return Reply(tool_calls=[("transfer_to_billing", {})])
    return Reply("Thank you for calling Lakeside Family Clinic. How can I help?")


@pytest.fixture
async def billing():
    app = build_app(BillingAgentSettings(token=TOKEN, llm="off", _env_file=None))
    client = BillingAgentClient(
        "http://billing.test", TOKEN, transport=httpx.ASGITransport(app=app)
    )
    yield client
    await client.aclose()
    await app.state.request_handler.aclose()


@pytest.mark.parametrize(("verified", "expect"), [(True, "$185.40"), (False, "verify")])
async def test_billing_question_goes_over_a2a(billing, verified, expect):
    state = CallState(room_name="call-1")
    if verified:  # as set by a successful lookup_appointments
        state.mark_verified("P-1002", "James Whitfield")
    settings = AgentSettings(_env_file=None)
    handoff = HumanHandoff(None, settings, EventPublisher(None, CallRecord("pipeline", {})))
    agent = ReceptionistAgent(
        clinic_tools=llm.Toolset(id="clinic", tools=[]),
        billing=billing,
        handoff=handoff,
        language="auto",
    )
    with fake_job_context():
        async with AgentSession[CallState](llm=ScriptedLLM(script), userdata=state) as session:
            await session.start(agent)
            result = await session.run(user_input="Hi, what's my balance?")
            result.expect.contains_function_call(name="transfer_to_billing")
            result.expect.contains_agent_handoff(new_agent_type=BillingLiaisonAgent)
            result.expect.contains_function_call(name="ask_billing_department")
            replies = [e.item.text_content for e in result.events if e.type == "message"]
            assert any(expect.lower() in (r or "").lower() for r in replies), replies
    assert state.billing_context_id  # one A2A context per call, reused for follow-ups
