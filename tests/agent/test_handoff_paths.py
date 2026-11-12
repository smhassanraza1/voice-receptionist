"""Phone transfers, the operator hand-over and the billing round trip, with fake LiveKit pieces."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fakes import Reply, ScriptedLLM, last_item, last_user_text
from livekit import api, rtc
from livekit.agents import AgentSession, llm
from livekit.agents.llm import ToolError
from livekit.agents.testing import fake_job_context

from receptionist.agent import handoff as handoff_module
from receptionist.agent.agents import STAY_ON_THE_LINE, BillingLiaisonAgent, ReceptionistAgent
from receptionist.agent.events import CallRecord, EventPublisher
from receptionist.agent.handoff import HumanHandoff, transfer_targets
from receptionist.agent.settings import AgentSettings
from receptionist.agent.state import CallState


def done(value: object = None) -> asyncio.Future:
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut


class FakeRoom:
    def __init__(self, *participants: SimpleNamespace) -> None:
        self.remote_participants = {p.identity: p for p in participants}
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler) -> None:
        self.handlers[event].remove(handler)

    def emit(self, event: str, *args: object) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(*args)


class FakeJob:
    def __init__(self, room: FakeRoom, transfer: object = None) -> None:
        self.room = room
        self.transfer = transfer
        self.transferred_to: list[str] = []
        self.shutdowns: list[str] = []

    def transfer_sip_participant(self, participant, transfer_to: str, play_dialtone: bool):
        self.transferred_to.append(transfer_to)
        if isinstance(self.transfer, Exception):
            raise self.transfer
        return done(self.transfer)

    def shutdown(self, reason: str = "") -> None:
        self.shutdowns.append(reason)


class FakeSession:
    def __init__(self) -> None:
        self.tts = object()  # pipeline: announce() uses say()
        self.said: list[str] = []
        self.closed = False
        self.shut_down = False

    def say(self, text: str, **_: object) -> asyncio.Future:
        self.said.append(text)
        return done()

    def interrupt(self, force: bool = False) -> asyncio.Future:
        return done()

    async def aclose(self) -> None:
        self.closed = True

    def shutdown(self) -> None:
        self.shut_down = True


def sip_caller() -> SimpleNamespace:
    return SimpleNamespace(
        identity="sip-caller", name="", kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    )


def phone_call(transfer: object = None, **settings: str):
    job = FakeJob(FakeRoom(sip_caller()), transfer)
    session = FakeSession()
    state = CallState(room_name="call-1")
    ctx = SimpleNamespace(userdata=state, session=session, disallow_interruptions=lambda: None)
    handoff = HumanHandoff(
        job,  # type: ignore[arg-type]
        AgentSettings(_env_file=None, human_transfer_number="+15550109999", **settings),
        EventPublisher(None, CallRecord("pipeline", {})),
    )
    agent = SimpleNamespace(chat_ctx=llm.ChatContext.empty())
    return handoff, agent, ctx, job, session


@pytest.mark.parametrize(
    ("number", "cold", "warm"),
    [
        ("+15550109999", "tel:+15550109999", "+15550109999"),
        (" tel:+15550109999 ", "tel:+15550109999", "+15550109999"),
        ("TEL:+15550109999", "tel:+15550109999", "+15550109999"),
        ("sip:frontdesk@pbx.example.com", "sip:frontdesk@pbx.example.com", None),
    ],
)
def test_transfer_number_is_accepted_with_or_without_tel(number, cold, warm):
    assert transfer_targets(number) == (cold, warm or cold)


async def test_cold_transfer_dials_a_tel_uri_and_ends_the_session():
    handoff, agent, ctx, job, session = phone_call(transfer=api.SIPParticipantInfo())
    assert await handoff.to_human(agent, ctx, reason="r", summary="s") is None
    assert job.transferred_to == ["tel:+15550109999"]
    assert session.shut_down and ctx.userdata.escalated


async def test_cold_transfer_reports_a_failed_status_to_the_caller():
    failed = api.TransferSIPParticipantResponse(
        status=api.SIPTransferStatus.STS_TRANSFER_FAILED,
        reason=api.SIPTransferReason.STR_REJECTED,
    )
    handoff, agent, ctx, job, session = phone_call(transfer=failed)
    with pytest.raises(ToolError):
        await handoff.to_human(agent, ctx, reason="r", summary="s")
    assert not ctx.userdata.escalated  # the caller can be offered a transfer again
    assert not session.shut_down


async def test_cold_transfer_error_resets_the_escalation():
    handoff, agent, ctx, _, session = phone_call(transfer=RuntimeError("trunk down"))
    with pytest.raises(ToolError):
        await handoff.to_human(agent, ctx, reason="r", summary="s")
    assert not ctx.userdata.escalated and not session.shut_down


async def test_warm_transfer_dials_a_bare_number(monkeypatch):
    dialled: list[str] = []

    def task(*, sip_call_to: str, **_: object) -> asyncio.Future:
        dialled.append(sip_call_to)
        return done()

    monkeypatch.setattr(handoff_module, "WarmTransferTask", task)
    handoff, agent, ctx, _, session = phone_call(
        human_transfer_mode="warm", LIVEKIT_SIP_OUTBOUND_TRUNK="ST_123"
    )
    handoff._settings.human_transfer_number = "tel:+15550109999"
    await handoff.to_human(agent, ctx, reason="r", summary="s")
    assert dialled == ["+15550109999"] and session.shut_down


async def test_warm_transfer_crash_resets_the_escalation(monkeypatch):
    def task(**_: object) -> None:
        raise RuntimeError("could not create the human agent session")

    monkeypatch.setattr(handoff_module, "WarmTransferTask", task)
    handoff, agent, ctx, _, session = phone_call(
        human_transfer_mode="warm", LIVEKIT_SIP_OUTBOUND_TRUNK="ST_123"
    )
    with pytest.raises(RuntimeError):
        await handoff.to_human(agent, ctx, reason="r", summary="s")
    assert not ctx.userdata.escalated and not session.shut_down


def operator(identity: str = "operator-1", name: str = "Dana") -> SimpleNamespace:
    return SimpleNamespace(identity=identity, name=name)


async def test_operator_hand_over_says_goodbye_once_and_ends_the_job():
    room = FakeRoom()
    job = FakeJob(room)
    session = FakeSession()
    pub = EventPublisher(None, CallRecord("pipeline", {}))
    handoff = HumanHandoff(job, AgentSettings(_env_file=None), pub)  # type: ignore[arg-type]
    handoff.watch(session)  # type: ignore[arg-type]

    room.emit("participant_connected", SimpleNamespace(identity="browser-caller", name=""))
    assert handoff._hand_over_task is None
    room.emit("participant_connected", operator())
    room.emit("participant_connected", operator("operator-2", "Sam"))
    await handoff._hand_over_task

    assert session.said == ["You're now connected with Dana from our front desk. Take care!"]
    assert session.closed and job.shutdowns == ["operator took over"]
    assert [h["reason"] for h in pub.record.handoffs] == ["operator joined"]


async def test_operator_hand_over_failure_is_logged_and_still_ends_the_job(caplog):
    room = FakeRoom()
    job = FakeJob(room)
    session = FakeSession()

    def interrupt(force: bool = False) -> None:
        raise RuntimeError("session already closing")

    session.interrupt = interrupt  # type: ignore[method-assign]
    handoff = HumanHandoff(
        job,  # type: ignore[arg-type]
        AgentSettings(_env_file=None),
        EventPublisher(None, CallRecord("pipeline", {})),
    )
    handoff.watch(session)  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="receptionist.agent"):
        room.emit("participant_connected", operator())
        await handoff._hand_over_task  # does not raise
    assert "operator hand-over failed" in caplog.text
    assert job.shutdowns == ["operator took over"]


# -- billing round trip ----------------------------------------------------------------------


def names(tools) -> set[str]:
    return {t.info.name for t in tools if hasattr(t, "info")}


def script(ctx: llm.ChatContext, tools) -> Reply:
    item = last_item(ctx)
    available = names(tools)
    if item.type == "function_call_output":
        return Reply(item.output if item.name == "ask_billing_department" else "One moment.")
    said = last_user_text(ctx).lower()
    if "ask_billing_department" in available:
        if "appointment" in said:
            return Reply(tool_calls=[("return_to_reception", {})])
        return Reply(tool_calls=[("ask_billing_department", {"question": said})])
    if "balance" in said:
        return Reply(tool_calls=[("transfer_to_billing", {})])
    return Reply("Is there anything else I can help with?")


class FakeBilling:
    def __init__(self) -> None:
        self.questions: list[str] = []

    async def ask(self, question: str, **_: object) -> SimpleNamespace:
        self.questions.append(question)
        return SimpleNamespace(text="Your balance is $185.40.", context_id="ctx-1")


async def test_billing_hands_back_to_reception_with_the_clinic_tools():
    state = CallState(room_name="call-1")
    state.mark_verified("P-1002", "James Whitfield")
    clinic_tools = llm.Toolset(id="clinic", tools=[])
    billing = FakeBilling()
    handoff = HumanHandoff(
        None, AgentSettings(_env_file=None), EventPublisher(None, CallRecord("pipeline", {}))
    )
    agent = ReceptionistAgent(
        clinic_tools=clinic_tools,
        billing=billing,  # type: ignore[arg-type]
        handoff=handoff,
        language="auto",
    )
    with fake_job_context():
        async with AgentSession[CallState](llm=ScriptedLLM(script), userdata=state) as session:
            await session.start(agent)
            result = await session.run(user_input="Hi, what's my balance?")
            result.expect.contains_agent_handoff(new_agent_type=BillingLiaisonAgent)

            result = await session.run(user_input="Thanks. Can I also book an appointment?")
            result.expect.contains_function_call(name="return_to_reception")
            result.expect.contains_agent_handoff(new_agent_type=ReceptionistAgent)
            back = session.current_agent
            assert isinstance(back, ReceptionistAgent) and back is not agent
            assert back._clinic_tools is clinic_tools and back._greet is False
            replies = [e.item.text_content for e in result.events if e.type == "message"]
            assert "Is there anything else I can help with?" in replies, replies
    assert billing.questions and state.billing_context_id == "ctx-1"


# -- hanging up ----------------------------------------------------------------------------


def hang_up(ctx: llm.ChatContext, tools) -> Reply:
    item = last_item(ctx)
    if item.type == "function_call_output":
        return Reply("They'll be right with you.")
    return Reply(tool_calls=[("end_call", {})])


async def test_end_call_is_refused_while_a_staff_member_is_on_the_way():
    state = CallState(room_name="call-1", escalated=True)
    handoff = HumanHandoff(
        None, AgentSettings(_env_file=None), EventPublisher(None, CallRecord("pipeline", {}))
    )
    agent = ReceptionistAgent(
        clinic_tools=llm.Toolset(id="clinic", tools=[]),
        billing=FakeBilling(),  # type: ignore[arg-type]
        handoff=handoff,
        language="auto",
    )
    with fake_job_context():
        async with AgentSession[CallState](llm=ScriptedLLM(hang_up), userdata=state) as session:
            await session.start(agent)
            result = await session.run(user_input="No, that's all. I'll wait.")
            result.expect.contains_function_call(name="end_call")
            result.expect.contains_function_call_output(output=STAY_ON_THE_LINE)
