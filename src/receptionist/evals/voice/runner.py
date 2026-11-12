"""Voice channel: persona scenarios over real audio through LiveKit.

The simulated caller's lines are synthesized to speech and sent over WebRTC; the agent's
replies come back as audio (timed) and as its own transcription stream (fed to the
simulator). Tool calls are read from the agent's server-side call record after hang-up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from livekit import rtc
from livekit.agents import llm

from receptionist.agent.settings import AgentSettings

from ..architectures import ArchName, call_config, model_label
from ..cost import usage_record
from ..grading import grade, snapshot_outcomes
from ..llm_utils import metered
from ..results import ScenarioResult
from ..scenarios import Scenario
from ..scoring import Event, format_conversation
from ..services import EvalServices
from ..simulator import CallerSimulator
from .audio import synthesize
from .caller import VoiceCaller
from .probes import Probe, ProbeRun, render_speech, run_probes
from .stack import AgentWorker, LocalLiveKit

logger = logging.getLogger("receptionist.evals.voice")

CALLER_VOICES = {
    "en": "alloy",
    "es": "nova",
    "fr": "shimmer",
    "de": "onyx",
    "hi": "sage",
    "ja": "coral",
}


def _dispatch_metadata(arch: ArchName, language: str) -> dict[str, str]:
    config = call_config(arch, language)
    return {
        "mode": config.mode,
        "language": config.language,
    }


def _tool_events(record: dict | None) -> list[Event]:
    events: list[Event] = []
    for call in (record or {}).get("tool_calls", []):
        at = call.get("at", 0.0)
        events.append(Event("tool_call", name=call["name"], args=call.get("arguments", {}), at=at))
        events.append(
            Event(
                "tool_result",
                call.get("output", ""),
                name=call["name"],
                is_error=call["status"] != "ok",
                at=at,
            )
        )
    for handoff in (record or {}).get("handoffs", []):
        events.append(Event("handoff", name=handoff["target"], at=handoff.get("at", 0.0)))
    return events


async def run_voice_scenario(
    scenario: Scenario,
    arch: ArchName,
    *,
    services: EvalServices,
    livekit: LocalLiveKit,
    worker: AgentWorker,
    settings: AgentSettings,
    simulator_llm: llm.LLM,
    judge_llm: llm.LLM | None,
    speech: Callable[..., bytes] = synthesize,
) -> ScenarioResult:
    started = time.perf_counter()
    language = scenario.persona.language if scenario.persona.language != "en" else "auto"
    room = livekit.new_room()
    token = livekit.token(
        f"caller-{scenario.id}", room, dispatch_metadata=_dispatch_metadata(arch, language)
    )
    voice = CALLER_VOICES.get(scenario.persona.language, "alloy")

    simulator = CallerSimulator(simulator_llm, scenario)
    befores: list[list[dict[str, Any]]] = []
    conversation: list[Event] = []
    ttfa: list[float] = []
    error: str | None = None
    with metered(simulator_llm, judge_llm) as overhead:
        try:
            services.reset()
            befores = await snapshot_outcomes(services, scenario)
            async with VoiceCaller(livekit.url, token) as caller:
                await asyncio.wait_for(caller.agent_joined.wait(), timeout=30)
                await caller.wait_for_agent_turn(after=0.0, timeout=30)
                conversation.append(Event("agent", caller.agent_text_since(0.0), at=time.time()))

                for _ in range(scenario.max_turns):
                    line = await simulator.next_turn(format_conversation(conversation))
                    if (
                        line is None
                        or caller.room.connection_state != rtc.ConnectionState.CONN_CONNECTED
                    ):
                        break
                    conversation.append(Event("caller", line, at=time.time()))
                    pcm = await asyncio.to_thread(speech, line, voice=voice)
                    said = await caller.say(pcm)
                    onset, _ = await caller.wait_for_agent_turn(after=said.ended_at, timeout=30)
                    if onset is not None:
                        ttfa.append(round((onset - said.ended_at) * 1000, 1))
                    await asyncio.sleep(0.3)  # the transcript stream closes just after the audio
                    reply = caller.agent_text_since(said.ended_at)
                    conversation.append(Event("agent", reply or "(no reply)", at=time.time()))
        except Exception as e:
            logger.exception("voice scenario %s failed", scenario.id)
            error = f"{type(e).__name__}: {e}"

        record: dict | None = None
        try:
            record = await asyncio.to_thread(worker.call_record, room)
        except Exception as e:
            logger.exception("call record for %s unreadable", scenario.id)
            error = error or f"call record unreadable: {type(e).__name__}: {e}"
        events = sorted(conversation + _tool_events(record), key=lambda e: e.at)
        server_e2e = [
            t["e2e_latency_ms"] for t in (record or {}).get("turns", []) if t.get("e2e_latency_ms")
        ]
        result = await grade(
            scenario,
            events,
            services=services,
            befores=befores,
            judge_llm=judge_llm,
            arch=arch,
            channel="voice",
            model=model_label(arch, settings),
            duration_s=time.perf_counter() - started,
            error=error,
            latency_ms={"ttfa": ttfa, "server_e2e": server_e2e},
            interruption={
                "interruptions": (record or {}).get("interruptions"),
                "false_interruptions": (record or {}).get("false_interruptions"),
            },
        )
    # the STT label records the language code it ran with, e.g. "deepgram/nova-3:multi"
    stt_label = ((record or {}).get("models") or {}).get("stt", "")
    result.usage = usage_record(
        (record or {}).get("usage", []),
        overhead=[u.model_dump() for u in overhead.flatten()],
        stt_language=stt_label.rpartition(":")[2] if ":" in stt_label else None,
    )
    return result


async def run_probe_calls(
    arch: ArchName,
    probes: list[Probe],
    *,
    livekit: LocalLiveKit,
    calls: int,
    speech: Callable[[str], bytes] = synthesize,
) -> list[ProbeRun]:
    # Uncached synthesis can take minutes. Done inside a call, the agent would hear a silent
    # caller and its silence check-in would hang up before the first probe.
    rendered = await render_speech(probes, speech)
    runs: list[ProbeRun] = []
    for i in range(calls):
        room = livekit.new_room()
        token = livekit.token(
            f"prober-{i}", room, dispatch_metadata=_dispatch_metadata(arch, "auto")
        )
        async with VoiceCaller(livekit.url, token) as caller:
            runs.append(await run_probes(caller, probes, arch=arch, speech=rendered.__getitem__))
    return runs
