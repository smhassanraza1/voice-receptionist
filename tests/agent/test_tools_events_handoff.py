import json
from types import SimpleNamespace

import httpx
import mcp.types as mcp_types
from livekit.agents import llm, mcp
from livekit.agents.voice.events import (
    ConversationItemAddedEvent,
    ToolCallEnded,
    ToolCallStarted,
    ToolExecutionUpdatedEvent,
    UserStateChangedEvent,
)

from receptionist.agent.events import CallRecord, EventPublisher, redact
from receptionist.agent.handoff import HumanHandoff, announce
from receptionist.agent.settings import AgentSettings
from receptionist.agent.state import CallState
from receptionist.agent.tools import make_result_resolver


def state() -> CallState:
    return CallState(room_name="call-1")


def tool_result(payload: dict) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
    )


def test_identity_comes_only_from_verified_tool_results():
    s = state()
    resolve = make_result_resolver(s)
    payload = {
        "patient_id": "P-1002",
        "patient_name": "James Whitfield",
        "identity_verified": True,
        "appointments": [],
    }
    # a search result can't verify anyone, even if it mentions a patient id
    resolve(
        mcp.MCPToolResultContext(
            "search_knowledge_base",
            {},
            tool_result({"patient_id": "P-9", "identity_verified": True}),
        )
    )
    assert s.verified_patient_id is None
    # nor can an identity tool's result unless the server explicitly says the caller is verified
    unflagged = {k: v for k, v in payload.items() if k != "identity_verified"}
    resolve(mcp.MCPToolResultContext("lookup_appointments", {}, tool_result(unflagged)))
    assert s.verified_patient_id is None
    text = resolve(
        mcp.MCPToolResultContext(
            "lookup_appointments", {"date_of_birth": "x"}, tool_result(payload)
        )
    )
    assert s.verified_patient_id == "P-1002" and s.verified_patient_name == "James Whitfield"
    assert text == json.dumps(payload, separators=(",", ":"))  # compact for the LLM


def test_booking_only_verifies_when_the_server_says_so():
    s = state()
    resolve = make_result_resolver(s)
    booking = {"patient_id": "P-1001", "confirmation_code": "LKC-AAAA"}
    resolve(
        mcp.MCPToolResultContext(
            "book_appointment", {}, tool_result({**booking, "identity_verified": False})
        )
    )
    assert s.verified_patient_id is None
    resolve(
        mcp.MCPToolResultContext(
            "book_appointment", {}, tool_result({**booking, "identity_verified": True})
        )
    )
    assert s.verified_patient_id == "P-1001"


def test_events_turn_metrics_interruptions_and_redacted_tools():
    record = CallRecord(mode="pipeline", models={})
    pub = EventPublisher(None, record)
    sent: list[dict] = []
    pub.publish = sent.append  # type: ignore[method-assign]

    user = llm.ChatMessage(role="user", content=["hi"], metrics={"end_of_turn_delay": 0.31})
    agent = llm.ChatMessage(
        role="assistant",
        content=["hello"],
        interrupted=True,
        metrics={"e2e_latency": 0.82, "llm_node_ttft": 0.4, "tts_node_ttfb": 0.11},
    )
    pub._on_item(ConversationItemAddedEvent(item=user))
    pub._on_item(ConversationItemAddedEvent(item=agent))
    assert record.interruptions == 1
    turn = next(e for e in sent if e["type"] == "turn_metrics")
    assert turn == {
        "type": "turn_metrics",
        "turn": 1,
        "e2e_latency_ms": 820,
        "eou_delay_ms": 310,
        "llm_ttft_ms": 400,
        "tts_ttfb_ms": 110,
        "interrupted": True,
    }

    call = llm.FunctionCall(
        call_id="c1",
        name="book_appointment",
        arguments=json.dumps({"phone": "+1555", "slot_id": "S"}),
    )
    pub._on_tool(ToolExecutionUpdatedEvent(update=ToolCallStarted(function_call=call)))
    pub._on_tool(
        ToolExecutionUpdatedEvent(
            update=ToolCallEnded(id="c1", call_id="c1", status="done", message="{}")
        )
    )
    started = next(e for e in sent if e["type"] == "tool_call" and e["status"] == "started")
    assert started["arguments"] == {"phone": "***", "slot_id": "S"}  # the browser never sees PII
    assert record.tool_calls[0]["arguments"]["phone"] == "+1555"  # the server-side record does
    assert record.tool_calls[0]["status"] == "ok"
    assert redact({"date_of_birth": "", "x": 1}) == {"date_of_birth": "", "x": 1}


def test_realtime_turn_latency_falls_back_to_caller_silence():
    """Realtime replies after a tool call carry no e2e_latency; time them from the caller."""
    record = CallRecord(mode="realtime", models={})
    pub = EventPublisher(None, record)
    sent: list[dict] = []
    pub.publish = sent.append  # type: ignore[method-assign]

    greeting = llm.ChatMessage(role="assistant", content=["hi"], metrics={"started_speaking_at": 5})
    pub._on_item(ConversationItemAddedEvent(item=greeting))
    assert record.turns == []  # nobody spoke before the greeting

    pub._on_user_state(
        UserStateChangedEvent(old_state="speaking", new_state="listening", created_at=100.0)
    )
    reply = llm.ChatMessage(
        role="assistant", content=["yes"], metrics={"started_speaking_at": 101.25}
    )
    pub._on_item(ConversationItemAddedEvent(item=reply))
    assert [t["e2e_latency_ms"] for t in record.turns] == [1250]

    follow_up = llm.ChatMessage(
        role="assistant", content=["anything else?"], metrics={"started_speaking_at": 104.0}
    )
    pub._on_item(ConversationItemAddedEvent(item=follow_up))
    assert len(record.turns) == 1  # one caller turn, one measurement


def test_announce_uses_tts_or_asks_the_realtime_model():
    calls: list[tuple[str, str, dict]] = []

    def session(tts: object) -> SimpleNamespace:
        return SimpleNamespace(
            tts=tts,
            say=lambda text, **kw: calls.append(("say", text, kw)),
            generate_reply=lambda **kw: calls.append(("generate_reply", kw["instructions"], kw)),
        )

    announce(session(tts=object()), "Please hold.")  # pipeline
    announce(session(tts=None), "Please hold.")  # speech-to-speech: no TTS, say() would raise
    assert calls[0] == ("say", "Please hold.", {"allow_interruptions": False})
    assert calls[1][0] == "generate_reply"
    assert '"Please hold."' in calls[1][1]
    assert calls[1][2]["tool_choice"] == "none"


def _run_ctx(s: CallState) -> SimpleNamespace:
    said: list[str] = []
    session = SimpleNamespace(say=lambda text, **_: said.append(text), shutdown=lambda: None)
    return SimpleNamespace(
        userdata=s, session=session, disallow_interruptions=lambda: None, said=said
    )


async def test_browser_handoff_files_an_escalation_once():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "esc-1"})

    settings = AgentSettings(
        _env_file=None, internal_api_token="secret-token", web_base_url="http://web.test"
    )
    pub = EventPublisher(None, CallRecord(mode="pipeline", models={}))
    handoff = HumanHandoff(None, settings, pub, http_transport=httpx.MockTransport(handler))
    s = state()
    msg = await handoff.to_human(
        None, _run_ctx(s), reason="asked for a person", summary="Carla, records letter"
    )
    assert "will join" in msg and s.escalated
    assert seen[0].headers["X-Internal-Token"] == "secret-token"
    assert json.loads(seen[0].content) == {
        "room": "call-1",
        "reason": "asked for a person",
        "summary": "Carla, records letter",
    }
    assert pub.record.handoffs[0]["target"] == "human"
    again = await handoff.to_human(None, _run_ctx(s), reason="x", summary="y")
    assert "already" in again and len(seen) == 1


async def test_handoff_degrades_gracefully_when_paging_is_down():
    settings = AgentSettings(_env_file=None, web_base_url="http://web.test")
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    handoff = HumanHandoff(
        None, settings, EventPublisher(None, CallRecord("pipeline", {})), http_transport=transport
    )
    s = state()
    msg = await handoff.to_human(None, _run_ctx(s), reason="r", summary="s")
    assert "unavailable" in msg and not s.escalated  # the caller can be offered it again
