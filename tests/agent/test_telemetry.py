import json
from types import SimpleNamespace

import pytest
from fakes import Reply, ScriptedLLM
from langsmith.integrations.livekit import LiveKitLangSmithSpanProcessor
from livekit.agents import NOT_GIVEN, Agent, AgentSession, function_tool
from livekit.agents.telemetry.traces import tracer as livekit_tracer
from livekit.agents.testing import fake_job_context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from receptionist.agent import telemetry

ENV = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_RECORD_AUDIO",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    # keep the provider out of LiveKit's global tracer, so other tests' spans export nowhere
    monkeypatch.setattr(telemetry, "set_tracer_provider", lambda provider: None)


@pytest.fixture
def langsmith_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")


def _processors(tracing: telemetry.Tracing) -> list:
    return list(tracing.provider._active_span_processor._span_processors)


def _job_ctx(*, enable_recording: bool = False, enable_redaction: bool = False) -> SimpleNamespace:
    job = SimpleNamespace(
        id="AJ_123",
        metadata=None,
        enable_recording=enable_recording,
        enable_redaction=enable_redaction,
    )
    return SimpleNamespace(job=job, room=SimpleNamespace(name="call-abc"))


def test_tracing_off_without_configuration():
    assert telemetry.setup_tracing() is None


def test_langsmith_key_alone_does_not_enable_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    assert telemetry.setup_tracing() is None


def test_langsmith_only(langsmith_env):
    tracing = telemetry.setup_tracing()
    try:
        assert isinstance(tracing.langsmith, LiveKitLangSmithSpanProcessor)
        assert tracing.record_audio
        assert not any(isinstance(p, BatchSpanProcessor) for p in _processors(tracing))
    finally:
        tracing.provider.shutdown()


def test_langsmith_and_langfuse_export_side_by_side(langsmith_env, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    tracing = telemetry.setup_tracing()
    try:
        processors = _processors(tracing)
        assert tracing.langsmith in processors
        assert "LangfuseSpanProcessor" in {type(p).__name__ for p in processors}
    finally:
        tracing.provider.shutdown()


def test_otlp_only_has_no_langsmith_processor(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    tracing = telemetry.setup_tracing()
    try:
        assert tracing.langsmith is None
        assert any(type(p) is BatchSpanProcessor for p in _processors(tracing))
        assert tracing.record_options(_job_ctx()) is NOT_GIVEN
    finally:
        tracing.provider.shutdown()


def test_audio_recording_leaves_livekit_cloud_uploads_to_the_project(langsmith_env):
    tracing = telemetry.setup_tracing()
    try:
        assert tracing.record_options(_job_ctx(enable_recording=False)) == {
            "audio": True,
            "traces": False,
            "logs": False,
            "transcript": False,
        }
        assert tracing.record_options(_job_ctx(enable_recording=True)) == {
            "audio": True,
            "traces": True,
            "logs": True,
            "transcript": True,
        }
        # LiveKit refuses audio without the transcript when the project redacts
        assert tracing.record_options(_job_ctx(enable_redaction=True))["transcript"] is True
    finally:
        tracing.provider.shutdown()


def test_audio_recording_can_be_turned_off(langsmith_env, monkeypatch):
    monkeypatch.setenv("LANGSMITH_RECORD_AUDIO", "false")
    tracing = telemetry.setup_tracing()
    try:
        assert not tracing.record_audio
        assert tracing.record_options(_job_ctx()) is NOT_GIVEN
    finally:
        tracing.provider.shutdown()


@pytest.mark.parametrize("realtime", [True, False])
def test_watch_captures_user_transcript_only_for_realtime(monkeypatch, realtime):
    instrumented: list[tuple[object, str]] = []
    threads: list[str] = []
    processor = SimpleNamespace(
        instrument_session=lambda session, thread_id: instrumented.append((session, thread_id))
    )
    monkeypatch.setattr(
        "langsmith.integrations.livekit.set_thread_id", lambda thread_id: threads.append(thread_id)
    )
    session = SimpleNamespace(on=lambda event, callback: None)
    telemetry.Tracing(provider=None, langsmith=processor).watch(
        _job_ctx(), session, realtime=realtime
    )
    assert threads == ["AJ_123"]
    assert instrumented == ([(session, "AJ_123")] if realtime else [])


def test_call_attributes_tag_spans_for_langsmith(monkeypatch):
    monkeypatch.setattr(telemetry, "get_job_context", lambda required=False: _job_ctx())
    attributes: dict[str, str] = {}
    span = SimpleNamespace(name="agent_turn", set_attribute=attributes.__setitem__)
    telemetry._CallAttributes().on_start(span)
    assert attributes["langsmith.metadata.mode"] == "realtime"
    assert attributes["langsmith.metadata.room"] == "call-abc"
    assert attributes["langfuse.session.id"] == "call-abc"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("llm_node", "span"), ("llm_request_run", "span"), ("llm_request", None)],
)
def test_only_the_model_request_is_a_langfuse_generation(monkeypatch, name, expected):
    monkeypatch.setattr(telemetry, "get_job_context", lambda required=False: _job_ctx())
    attributes: dict[str, str] = {}
    span = SimpleNamespace(name=name, set_attribute=attributes.__setitem__)
    telemetry._CallAttributes().on_start(span)
    assert attributes.get("langfuse.observation.type") == expected


class _Receptionist(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a clinic receptionist.")

    @function_tool
    async def check_slots(self, day: str) -> str:
        """Check open slots."""
        return "10:00 open"


def _book(chat_ctx, tools) -> Reply:
    if getattr(chat_ctx.items[-1], "type", "") == "function_call_output":
        return Reply(text="You're booked for Tuesday at 10.")
    return Reply(tool_calls=[("check_slots", {"day": "tuesday"})])


async def test_call_trace_is_released_when_the_session_closes():
    exported = InMemorySpanExporter()
    processor = LiveKitLangSmithSpanProcessor(SimpleSpanProcessor(exported), recording_mode="none")
    provider = TracerProvider()
    tracing = telemetry.Tracing(provider, langsmith=processor)
    provider.add_span_processor(tracing.call_attributes)
    provider.add_span_processor(processor)
    previous = livekit_tracer._tracer_provider
    livekit_tracer.set_provider(provider)  # restored below, so other tests are unaffected
    try:
        with fake_job_context(job_id="AJ_call") as ctx:
            session = AgentSession(llm=ScriptedLLM(_book))
            tracing.watch(ctx, session, realtime=False)
            await session.start(_Receptionist())
            await session.run(user_input="Can I book Tuesday?")
            await session.aclose()
        provider.force_flush()
    finally:
        livekit_tracer.set_provider(previous)
        provider.shutdown()

    spans = {s.name: s for s in exported.get_finished_spans()}
    # the root holds the call together; it must not wait for the release timeout
    root = spans["agent_session"].attributes
    assert root["langsmith.metadata.thread_id"] == "AJ_call"
    assert root["langsmith.metadata.mode"] == "realtime"
    assert root["langfuse.trace.name"] == "receptionist-call"
    assert json.loads(root["langfuse.observation.input"]) == [
        {"role": "user", "content": "Can I book Tuesday?"}
    ]
    assert json.loads(root["langfuse.observation.output"])[-1] == {
        "role": "assistant",
        "content": "You're booked for Tuesday at 10.",
    }
    tool = spans["function_tool"].attributes
    assert tool["langsmith.span.kind"] == "tool" and tool["gen_ai.tool.name"] == "check_slots"


def test_conversation_goes_on_the_job_root_when_there_is_one(monkeypatch):
    monkeypatch.setattr(telemetry, "get_job_context", lambda required=False: _job_ctx())
    attributes: dict[str, dict] = {"job_entrypoint": {}, "agent_session": {}}
    call = telemetry._CallAttributes()
    for name in ("job_entrypoint", "agent_session"):  # the worker starts them in this order
        call.on_start(SimpleNamespace(name=name, set_attribute=attributes[name].__setitem__))
    history = SimpleNamespace(
        messages=lambda: [SimpleNamespace(role="user", text_content="Are you open Saturday?")]
    )
    call.record_conversation("AJ_123", history)
    assert "langfuse.observation.input" in attributes["job_entrypoint"]
    assert "langfuse.observation.input" not in attributes["agent_session"]


@pytest.mark.parametrize(("name", "flushes"), [("job_entrypoint", True), ("agent_session", False)])
def test_provider_is_flushed_when_the_job_root_ends(name, flushes):
    flushed: list[int] = []
    provider = SimpleNamespace(force_flush=lambda timeout_millis: flushed.append(timeout_millis))
    telemetry._FlushWhenCallEnds(provider).on_end(SimpleNamespace(name=name))
    assert bool(flushed) is flushes


def test_flush_at_call_end_runs_after_every_exporter(langsmith_env, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    tracing = telemetry.setup_tracing()
    try:
        assert isinstance(_processors(tracing)[-1], telemetry._FlushWhenCallEnds)
    finally:
        tracing.provider.shutdown()
