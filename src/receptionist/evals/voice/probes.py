"""Scripted voice probes: time to first audio, barge-in, and backchannel handling.

The same caller audio is played to every architecture, so differences come from the agent.

- ttfa: caller stops speaking -> first agent audio the caller hears.
- barge-in: the caller talks over the agent; stop latency is caller-starts-talking ->
  agent audio stops. A stop that never comes within the window counts as a miss.
- backchannel: the caller says "mm-hmm" mid-reply. The agent should keep talking; stopping
  for good is a false interruption.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from ..stats import percentile
from .audio import synthesize
from .caller import VoiceCaller

STOP_WINDOW_S = 4.0
RESUME_WINDOW_S = 3.0


class Overlap(BaseModel):
    after_ms: int = 1500
    say: str


class Probe(BaseModel):
    id: str
    say: str
    interrupt: Overlap | None = None
    backchannel: Overlap | None = None


@dataclass
class ProbeResult:
    probe_id: str
    ttfa_ms: float | None = None
    stop_latency_ms: float | None = None
    interrupted: bool | None = None
    backchannel_outcome: Literal["continued", "paused_resumed", "stopped"] | None = None
    reply_ttfa_ms: float | None = None
    agent_text: str = ""


@dataclass
class ProbeRun:
    arch: str
    greeting_ttfa_ms: float | None
    results: list[ProbeResult] = field(default_factory=list)
    server_turns: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def load_probes(path: Path) -> list[Probe]:
    return [Probe.model_validate(p) for p in yaml.safe_load(path.read_text())]


def _ms(start: float | None, end: float | None) -> float | None:
    return None if start is None or end is None else round((end - start) * 1000, 1)


async def render_speech(
    probes: list[Probe], speech: Callable[[str], bytes] = synthesize
) -> dict[str, bytes]:
    """Every caller line the probes speak, synthesized."""
    texts = {p.say for p in probes} | {
        o.say for p in probes for o in (p.interrupt, p.backchannel) if o
    }
    return {t: await asyncio.to_thread(speech, t) for t in texts}


async def run_probes(
    caller: VoiceCaller,
    probes: list[Probe],
    *,
    arch: str,
    speech: Callable[[str], bytes] = synthesize,
    turn_timeout: float = 25.0,
) -> ProbeRun:
    # render all caller audio up front: synthesis must never stall the live mic loop
    rendered = await render_speech(probes, speech)

    def audio(text: str) -> bytes:
        return rendered[text]

    await asyncio.wait_for(caller.agent_joined.wait(), timeout=30)
    joined = time.perf_counter()
    onset, _ = await caller.wait_for_agent_turn(after=0.0, timeout=turn_timeout)
    run = ProbeRun(arch=arch, greeting_ttfa_ms=_ms(joined, onset))

    for probe in probes:
        result = ProbeResult(probe_id=probe.id)
        utterance = await caller.say(audio(probe.say))
        overlap = probe.interrupt or probe.backchannel
        if overlap is None:
            onset, _ = await caller.wait_for_agent_turn(
                after=utterance.ended_at, timeout=turn_timeout
            )
            result.ttfa_ms = _ms(utterance.ended_at, onset)
        else:
            onset = await _wait_for_onset(caller, after=utterance.ended_at, window=turn_timeout)
            result.ttfa_ms = _ms(utterance.ended_at, onset)
            if onset is not None:
                await _sleep_until(onset + overlap.after_ms / 1000)
                # an agent that already finished its reply can't be interrupted: inconclusive
                if caller.agent_audio.speaking:
                    if probe.interrupt is not None:
                        await _barge_in(caller, probe.interrupt, result, audio, turn_timeout)
                    else:
                        await _backchannel(caller, overlap, result, audio, turn_timeout)
                else:
                    await caller.wait_for_agent_turn(after=utterance.ended_at, timeout=turn_timeout)

        result.agent_text = caller.agent_text_since(utterance.started_at)
        run.results.append(result)

    run.server_turns = [e for e in caller.log.events if e.get("type") == "turn_metrics"]
    return run


async def _barge_in(
    caller: VoiceCaller,
    overlap: Overlap,
    result: ProbeResult,
    speech: Callable[[str], bytes],
    timeout: float,
) -> None:
    barge = caller.start_speaking(speech(overlap.say))
    barge_started = time.perf_counter()
    stop = await _wait_for_offset(caller, after=barge_started, window=STOP_WINDOW_S)
    result.interrupted = stop is not None
    result.stop_latency_ms = _ms(barge_started, stop)
    spoken = await barge
    reply_onset, _ = await caller.wait_for_agent_turn(after=spoken.ended_at, timeout=timeout)
    result.reply_ttfa_ms = _ms(spoken.ended_at, reply_onset)


async def _backchannel(
    caller: VoiceCaller,
    overlap: Overlap,
    result: ProbeResult,
    speech: Callable[[str], bytes],
    timeout: float,
) -> None:
    said = await caller.say(speech(overlap.say))
    stop = await _wait_for_offset(caller, after=said.started_at, window=1.5)
    if stop is None:
        result.backchannel_outcome = "continued"
    else:
        resumed = await _wait_for_onset(caller, after=stop, window=RESUME_WINDOW_S)
        result.backchannel_outcome = "paused_resumed" if resumed else "stopped"
    await caller.wait_for_agent_turn(after=said.started_at, timeout=timeout)


async def _sleep_until(t: float) -> None:
    await asyncio.sleep(max(0.0, t - time.perf_counter()))


async def _wait_for_offset(caller: VoiceCaller, *, after: float, window: float) -> float | None:
    deadline = after + window
    while time.perf_counter() < deadline + caller.agent_audio.hangover_s:
        if (t := caller.agent_audio.first_offset_after(after)) is not None:
            return t if t <= deadline else None
        await asyncio.sleep(0.02)
    return None


async def _wait_for_onset(caller: VoiceCaller, *, after: float, window: float) -> float | None:
    deadline = after + window
    while time.perf_counter() < deadline:
        if (t := caller.agent_audio.first_onset_after(after + 0.001)) is not None:
            return t
        await asyncio.sleep(0.02)
    return None


def summarize(runs: list[ProbeRun]) -> dict:
    results = [r for run in runs for r in run.results]

    ttfa = [r.ttfa_ms for r in results if r.ttfa_ms is not None]
    stops = [r.stop_latency_ms for r in results if r.stop_latency_ms is not None]
    barge = [r for r in results if r.interrupted is not None]
    backchannel = [r for r in results if r.backchannel_outcome is not None]
    return {
        "calls": len(runs),
        "ttfa_ms": {
            "p50": percentile(ttfa, 0.5),
            "p90": percentile(ttfa, 0.9),
            "mean": round(statistics.mean(ttfa), 1) if ttfa else None,
            "n": len(ttfa),
        },
        "greeting_ms": {
            "p50": percentile([r.greeting_ttfa_ms for r in runs if r.greeting_ttfa_ms], 0.5)
        },
        "barge_in": {
            "stop_rate": round(sum(r.interrupted for r in barge) / len(barge), 3)
            if barge
            else None,
            "stop_latency_p50_ms": percentile(stops, 0.5),
            "stop_latency_p90_ms": percentile(stops, 0.9),
            "n": len(barge),
        },
        "backchannel": {
            "false_interruption_rate": (
                round(
                    sum(r.backchannel_outcome == "stopped" for r in backchannel) / len(backchannel),
                    3,
                )
                if backchannel
                else None
            ),
            "outcomes": {
                o: sum(r.backchannel_outcome == o for r in backchannel)
                for o in ("continued", "paused_resumed", "stopped")
            },
            "n": len(backchannel),
        },
    }
