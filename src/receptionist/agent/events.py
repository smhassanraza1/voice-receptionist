"""Streams call telemetry to the browser and keeps a per-call record.

Events are published on the `receptionist.events` data topic. The same numbers are kept in
`CallRecord` so the eval harness and logs see exactly what the UI shows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from livekit import rtc
from livekit.agents import AgentSession, llm
from livekit.agents.voice.events import (
    AgentFalseInterruptionEvent,
    ConversationItemAddedEvent,
    ToolCallEnded,
    ToolCallStarted,
    ToolExecutionUpdatedEvent,
    UserStateChangedEvent,
)

logger = logging.getLogger("receptionist.agent")

TOPIC = "receptionist.events"
SENSITIVE_ARGS = {"date_of_birth", "phone", "patient_name", "confirmation_code"}


def redact(arguments: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k in SENSITIVE_ARGS and v else v) for k, v in arguments.items()}


def _ms(seconds: float | None) -> int | None:
    return None if seconds is None else round(seconds * 1000)


@dataclass
class CallRecord:
    mode: str
    models: dict[str, str]
    started_at: float = field(default_factory=time.time)
    turns: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    interruptions: int = 0
    false_interruptions: int = 0
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)  # LiveKit per-model usage

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventPublisher:
    def __init__(self, room: rtc.Room | None, record: CallRecord) -> None:
        self._room = room
        self.record = record
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending_tools: dict[str, tuple[str, dict[str, Any], float]] = {}
        self._last_user_metrics: dict[str, Any] = {}
        self._user_stopped_at: float | None = None

    def publish(self, payload: dict[str, Any]) -> None:
        if self._room is None or not self._room.isconnected():
            return
        data = json.dumps(payload, separators=(",", ":")).encode()
        task = asyncio.create_task(
            self._room.local_participant.publish_data(data, reliable=True, topic=TOPIC)
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_published)

    def _on_published(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            logger.warning("could not publish a call event", exc_info=e)

    def attach(self, session: AgentSession) -> None:
        session.on("conversation_item_added", self._on_item)
        session.on("tool_execution_updated", self._on_tool)
        session.on("agent_false_interruption", self._on_false_interruption)
        session.on("user_state_changed", self._on_user_state)
        # the session closes before the job leaves the room, so the browser still gets this
        session.on("close", lambda _: self.usage_summary(session))

    def session_info(self, language: str) -> None:
        self.publish(
            {
                "type": "session_info",
                "mode": self.record.mode,
                "models": self.record.models,
                "language": language,
            }
        )

    def handoff(self, target: str, reason: str) -> None:
        self.record.handoffs.append({"target": target, "reason": reason, "at": time.time()})
        self.publish({"type": "handoff", "target": target, "reason": reason})

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        item = ev.item
        if not isinstance(item, llm.ChatMessage):
            return
        if item.role == "user":
            self._last_user_metrics = dict(item.metrics)
            return
        if item.role != "assistant":
            return

        if item.interrupted:
            self.record.interruptions += 1
            self.publish({"type": "interruption", "turn": len(self.record.turns)})

        m = item.metrics
        e2e = m.get("e2e_latency")
        started = m.get("started_speaking_at")
        stopped = self._user_stopped_at
        if e2e is None and stopped is not None and started is not None and started >= stopped:
            # LiveKit leaves e2e_latency unset on realtime replies that follow a tool call (and
            # whenever its own VAD bookkeeping misses the turn). Time from the user state leaving
            # "speaking" to first agent audio instead. With server-side turn detection that state
            # flips on the model's end-of-turn signal, so like LiveKit's own realtime e2e it leaves
            # out the model's end-of-turn wait; `evals probes` measures the full caller-side wait.
            e2e = started - stopped
        if e2e is None:
            return
        self._user_stopped_at = None
        turn = {
            "turn": len(self.record.turns) + 1,
            "e2e_latency_ms": _ms(e2e),
            "eou_delay_ms": _ms(self._last_user_metrics.get("end_of_turn_delay")),
            "transcription_delay_ms": _ms(self._last_user_metrics.get("transcription_delay")),
            "llm_ttft_ms": _ms(m.get("llm_node_ttft")),
            "tts_ttfb_ms": _ms(m.get("tts_node_ttfb")),
            "interrupted": item.interrupted,
        }
        self.record.turns.append(turn)
        self.publish({"type": "turn_metrics", **{k: v for k, v in turn.items() if v is not None}})

    def _on_user_state(self, ev: UserStateChangedEvent) -> None:
        if ev.old_state == "speaking" and ev.new_state != "speaking":
            self._user_stopped_at = ev.created_at

    def _on_tool(self, ev: ToolExecutionUpdatedEvent) -> None:
        update = ev.update
        if isinstance(update, ToolCallStarted):
            call = update.function_call
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            self._pending_tools[call.call_id] = (call.name, args, time.perf_counter())
            self.publish(
                {
                    "type": "tool_call",
                    "name": call.name,
                    "status": "started",
                    "arguments": redact(args),
                }
            )
        elif isinstance(update, ToolCallEnded):
            name, args, started = self._pending_tools.pop(
                update.call_id, ("unknown", {}, time.perf_counter())
            )
            status = "ok" if update.status == "done" else "error"
            duration_ms = round((time.perf_counter() - started) * 1000)
            # the record stays server-side (written only when RECEPTIONIST_CALL_LOG_DIR is
            # set, for evals); the browser only ever sees redacted arguments
            self.record.tool_calls.append(
                {
                    "name": name,
                    "status": status,
                    "duration_ms": duration_ms,
                    "arguments": args,
                    "output": (update.message or "")[:4000],
                    "at": time.time(),
                }
            )
            self.publish(
                {"type": "tool_call", "name": name, "status": status, "duration_ms": duration_ms}
            )

    def _on_false_interruption(self, ev: AgentFalseInterruptionEvent) -> None:
        self.record.false_interruptions += 1

    def usage_summary(self, session: AgentSession) -> None:
        self.record.usage = [u.model_dump() for u in session.usage.model_usage]
        turns = [
            t["e2e_latency_ms"] for t in self.record.turns if t.get("e2e_latency_ms") is not None
        ]
        summary = {
            "turns": len(self.record.turns),
            "median_e2e_latency_ms": sorted(turns)[len(turns) // 2] if turns else None,
            "tool_calls": len(self.record.tool_calls),
            "interruptions": self.record.interruptions,
            "false_interruptions": self.record.false_interruptions,
        }
        self.publish({"type": "usage", "summary": summary})
        logger.info("call summary", extra={"summary": summary, "usage": str(session.usage)})
