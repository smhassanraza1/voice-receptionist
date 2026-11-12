"""OpenTelemetry tracing for the agent, exported to LangSmith, Langfuse and/or any OTLP backend.

LiveKit Agents emits spans for every turn (user turn, LLM request, TTS, tool calls) with
latency attributes such as `lk.e2e_latency`. One tracer provider fans them out to every
configured backend:

- LangSmith (`LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY`): LangSmith's LiveKit processor
  turns each call into one trace with its transcript, tool calls, latency and token metrics,
  plus the call audio unless `LANGSMITH_RECORD_AUDIO=false` (on LiveKit Cloud that audio is also
  uploaded to the project's Agent Observability).
- Langfuse (`LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY`): the Langfuse SDK's span processor
  exports every span, grouped into one session per room.
- Any other OTLP backend (`OTEL_EXPORTER_OTLP_*`): the spans are exported as LiveKit emits them.

With none of these set, tracing stays off.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from livekit.agents import (
    NOT_GIVEN,
    AgentSession,
    JobContext,
    NotGivenOr,
    RecordingOptions,
    get_job_context,
    llm,
)
from livekit.agents.telemetry import set_tracer_provider
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .settings import CallConfig, get_settings

if TYPE_CHECKING:
    from langsmith.integrations.livekit import LiveKitLangSmithSpanProcessor

logger = logging.getLogger("receptionist.agent")


# LiveKit wraps each model request in these spans and puts the model name (and, on
# `llm_node`, the turn's token usage) on them too, so Langfuse would count every request
# up to three times as a priced generation. Only `llm_request` is the model call itself.
_LLM_WRAPPER_SPANS = frozenset({"llm_node", "llm_request_run"})
# A worker job's root span. It ends after the shutdown callbacks; in-process sessions (evals,
# tests) have none, and `agent_session` is their root.
_JOB_ROOT_SPAN = "job_entrypoint"


class _CallAttributes(SpanProcessor):
    """Tags every span of a call with its session and architecture, so traces can be grouped
    per call (a Langfuse session) and filtered by mode (LangSmith metadata), and keeps each
    call's root span so the conversation can be written onto it when the call ends."""

    def __init__(self) -> None:
        self._mode_by_job: dict[str, str] = {}
        self._root_by_job: dict[str, Span] = {}

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if (ctx := get_job_context(required=False)) is None:
            return
        job_id = ctx.job.id
        session_id = ctx.room.name or job_id
        span.set_attribute("langfuse.session.id", session_id)
        span.set_attribute("session.id", session_id)
        if job_id not in self._mode_by_job:
            config = CallConfig.from_metadata(ctx.job.metadata, get_settings())
            self._mode_by_job[job_id] = config.mode
        span.set_attribute("receptionist.mode", self._mode_by_job[job_id])
        span.set_attribute("langsmith.metadata.mode", self._mode_by_job[job_id])
        span.set_attribute("langsmith.metadata.room", ctx.room.name)
        if span.name in _LLM_WRAPPER_SPANS:
            span.set_attribute("langfuse.observation.type", "span")
        elif span.name in (_JOB_ROOT_SPAN, "agent_session") and job_id not in self._root_by_job:
            span.set_attribute("langfuse.trace.name", "receptionist-call")
            self._root_by_job[job_id] = span

    def record_conversation(self, job_id: str, history: llm.ChatContext) -> None:
        """Sets the call's input (the caller's first words) and output (the conversation)
        on its root span, which is what the Langfuse trace table shows for the call."""
        if (root := self._root_by_job.pop(job_id, None)) is None:
            return
        messages = [
            {"role": item.role, "content": item.text_content}
            for item in history.messages()
            if item.role in ("user", "assistant") and item.text_content
        ]
        first_user = next((m for m in messages if m["role"] == "user"), None)
        if first_user is not None:
            root.set_attribute("langfuse.observation.input", json.dumps([first_user]))
        root.set_attribute("langfuse.observation.output", json.dumps(messages))

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class _FlushWhenCallEnds(SpanProcessor):
    """Flushes the provider when a job's root span ends. That happens after the shutdown
    callbacks, and on Linux a job process exits without running exit handlers, so without
    this the root (and LangSmith's whole conversation, which it carries) is never sent.
    Registered last, so every exporter has queued the root by the time it runs."""

    def __init__(self, provider: TracerProvider) -> None:
        self._provider = provider

    def on_end(self, span: ReadableSpan) -> None:
        if span.name == _JOB_ROOT_SPAN:
            self._provider.force_flush(timeout_millis=10_000)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@dataclass
class Tracing:
    provider: TracerProvider
    langsmith: LiveKitLangSmithSpanProcessor | None = None
    record_audio: bool = False
    call_attributes: _CallAttributes = field(default_factory=_CallAttributes)

    def watch(self, ctx: JobContext, session: AgentSession, *, realtime: bool) -> None:
        """Call from the entrypoint, before `session.start`. Writes the conversation onto the
        call's trace when the session closes, and groups the call into one LangSmith thread.
        A realtime model's user transcript arrives as a session event rather than on a span,
        so for those LangSmith captures it from the session."""
        session.on(
            "close",
            lambda _: self.call_attributes.record_conversation(ctx.job.id, session.history),
        )
        if self.langsmith is None:
            return
        from langsmith.integrations.livekit import set_thread_id

        set_thread_id(ctx.job.id)  # unique per call; room names repeat in console mode
        if realtime:
            self.langsmith.instrument_session(session, ctx.job.id)

    def record_options(self, ctx: JobContext) -> NotGivenOr[RecordingOptions]:
        """Records the call audio for LangSmith. On LiveKit Cloud the recording is uploaded to
        the project's Agent Observability too, whatever the project's recording setting: the
        session report goes up whenever any recording option is on. Traces, logs and the
        transcript keep following the project's setting, except that a project with redaction
        on needs the transcript alongside the audio."""
        if self.langsmith is None or not self.record_audio:
            return NOT_GIVEN
        cloud = ctx.job.enable_recording
        transcript = cloud or ctx.job.enable_redaction
        return {"audio": True, "traces": cloud, "logs": cloud, "transcript": transcript}


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def setup_tracing(service_name: str = "voice-receptionist") -> Tracing | None:
    langsmith = _env_flag("LANGSMITH_TRACING", False) and bool(os.getenv("LANGSMITH_API_KEY"))
    langfuse = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
    otlp = bool(
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )
    if not (langsmith or langfuse or otlp):
        logger.info("tracing disabled (no LangSmith, Langfuse or OTLP configuration)")
        return None

    resource = {"service.name": service_name}
    if environment := os.getenv("LANGFUSE_TRACING_ENVIRONMENT"):
        # the Langfuse SDK only stamps it on providers it creates itself
        resource["langfuse.environment"] = environment
    provider = TracerProvider(resource=Resource.create(resource))
    tracing = Tracing(provider)
    provider.add_span_processor(tracing.call_attributes)
    if langsmith:
        from langsmith.integrations.livekit import LiveKitLangSmithSpanProcessor

        tracing.record_audio = _env_flag("LANGSMITH_RECORD_AUDIO", True)
        tracing.langsmith = LiveKitLangSmithSpanProcessor(
            # with audio off, the session report still supplies the chat history
            recording_mode="session_report" if tracing.record_audio else "none"
        )
        provider.add_span_processor(tracing.langsmith)
    if langfuse:
        from langfuse import Langfuse

        # reads LANGFUSE_BASE_URL; LiveKit's spans don't match Langfuse's default LLM-span
        # filter, so export them all
        Langfuse(tracer_provider=provider, should_export_span=lambda span: True)
    if otlp:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # reads the standard OTEL_EXPORTER_OTLP_* variables itself
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    provider.add_span_processor(_FlushWhenCallEnds(provider))
    set_tracer_provider(provider)
    return tracing
