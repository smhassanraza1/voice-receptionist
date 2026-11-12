"""Per-call cost model for both architectures, priced from `data/pricing.yaml`.

Speech-to-speech models bill the *whole conversation* again on every model pass (each user
turn, plus one extra pass per tool call), so cost grows faster than linearly with call length.
The simulation below walks the passes, tracks what each one re-sends, and prices it:

- OpenAI Realtime: what the previous request already sent is a cached prefix; new items
  (the latest user audio, the previous reply, tool items) are uncached.
- Pipeline: STT bills streamed minutes, the text LLM bills tokens (with prompt caching),
  TTS bills characters.

`tests/evals/test_cost.py` pins the defaults to hand-checked figures.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PRICING_PATH = Path(__file__).parent / "data" / "pricing.yaml"


@lru_cache
def load_pricing(path: Path = PRICING_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    data["by_id"] = {m["id"]: m for m in data["models"]}
    return data


def price(model_id: str, key: str) -> float:
    return float(load_pricing()["by_id"][model_id]["prices"][key])


@dataclass(frozen=True)
class CallProfile:
    duration_s: float = 180
    user_turns: int = 8
    user_speech_s: float = 81
    vad_padding_s: float = 0.5  # committed per user turn on top of speech
    agent_speech_s: float = 72
    tool_turns: tuple[int, ...] = (2, 4, 6)  # 1-based user turns that trigger one tool call
    system_tokens: int = 1500  # instructions + tool schemas
    tool_call_tokens: int = 40
    tool_result_tokens: int = 250
    user_text_tokens: int = 269
    agent_text_tokens: int = 239
    agent_characters: int = 1026
    reasoning_tokens_per_response: int = 0

    @property
    def minutes(self) -> float:
        return self.duration_s / 60


@dataclass
class _Item:
    kind: str  # "text" | "audio"
    tokens: float


def _passes(
    p: CallProfile, *, user_audio: float | None, agent_audio: float | None
) -> list[tuple[list[_Item], list[_Item]]]:
    """Context sent and items produced, per model pass.

    user_audio / agent_audio are tokens per turn when turns are carried as audio (realtime),
    or None when they are carried as text (pipeline LLM).
    """
    user_item = (
        _Item("audio", user_audio)
        if user_audio is not None
        else _Item("text", p.user_text_tokens / p.user_turns)
    )
    agent_item = (
        _Item("audio", agent_audio)
        if agent_audio is not None
        else _Item("text", p.agent_text_tokens / p.user_turns)
    )
    context: list[_Item] = [_Item("text", p.system_tokens)]
    passes: list[tuple[list[_Item], list[_Item]]] = []
    for turn in range(1, p.user_turns + 1):
        context.append(_Item(user_item.kind, user_item.tokens))
        if turn in p.tool_turns:
            call = _Item("text", p.tool_call_tokens)
            passes.append((list(context), [call]))
            context += [call, _Item("text", p.tool_result_tokens)]
        reply = _Item(agent_item.kind, agent_item.tokens)
        passes.append((list(context), [reply]))
        context.append(reply)
    return passes


def _sum(items: list[_Item], kind: str) -> float:
    return sum(i.tokens for i in items if i.kind == kind)


def openai_realtime(
    p: CallProfile, model_id: str = "openai/gpt-realtime-2", *, cache: bool = True
) -> dict[str, float]:
    rates = load_pricing()["audio_tokens_per_second"]
    out_per_reply = p.agent_speech_s / p.user_turns * rates["openai_realtime_output"]
    user_per_turn = (p.user_speech_s / p.user_turns + p.vad_padding_s) * rates[
        "openai_realtime_input"
    ]
    tokens = {"text_in": 0.0, "text_in_cached": 0.0, "audio_in": 0.0, "audio_in_cached": 0.0}
    previous: list[int] = []
    for context, _ in _passes(p, user_audio=user_per_turn, agent_audio=out_per_reply):
        ids = [id(i) for i in context]
        for item in context:
            cached = cache and id(item) in previous
            tokens[f"{item.kind}_in{'_cached' if cached else ''}"] += item.tokens
        previous = ids
    passes = p.user_turns + len(p.tool_turns)
    text_out = len(p.tool_turns) * p.tool_call_tokens + p.reasoning_tokens_per_response * passes
    audio_out = p.agent_speech_s * rates["openai_realtime_output"]
    cost = {
        "text_in": tokens["text_in"] * price(model_id, "text_input"),
        "text_in_cached": tokens["text_in_cached"] * price(model_id, "text_input_cached"),
        "audio_in": tokens["audio_in"] * price(model_id, "audio_input"),
        "audio_in_cached": tokens["audio_in_cached"] * price(model_id, "audio_input_cached"),
        "text_out": text_out * price(model_id, "text_output"),
        "audio_out": audio_out * price(model_id, "audio_output"),
    }
    cost = {k: v / 1e6 for k, v in cost.items()}
    # input transcription (LiveKit asks the realtime API for caller transcripts)
    cost["transcription"] = (
        p.user_speech_s / 60 * price("openai/gpt-4o-mini-transcribe", "per_minute_streaming")
    )
    return cost


def pipeline(
    p: CallProfile,
    *,
    stt: tuple[str, str] = ("deepgram/nova-3", "per_minute_streaming"),
    llm: str = "openai/gpt-4.1-mini",
    tts: str = "cartesia/sonic-3",
    cache: bool = True,
) -> dict[str, float]:
    uncached = cached = 0.0
    previous: list[int] = []
    for context, _ in _passes(p, user_audio=None, agent_audio=None):
        for item in context:
            if cache and id(item) in previous:
                cached += item.tokens
            else:
                uncached += item.tokens
        previous = [id(i) for i in context]
    output = p.agent_text_tokens + len(p.tool_turns) * p.tool_call_tokens
    return {
        "stt": p.minutes * price(stt[0], stt[1]),
        "llm": (
            uncached * price(llm, "input")
            + cached * price(llm, "cached_input")
            + output * price(llm, "output")
        )
        / 1e6,
        "tts": p.agent_characters * price(tts, "per_1m_characters") / 1e6,
    }


def infrastructure(p: CallProfile, *, phone: bool) -> dict[str, float]:
    cost = {"livekit_cloud": p.minutes * price("livekit/cloud-agent-session", "per_minute")}
    if phone:
        cost["sip"] = p.minutes * (
            price("twilio/elastic-sip-inbound-us", "per_minute")
            + price("livekit/sip", "per_minute")
        )
    return cost


def _pricing_entry(model: str) -> dict[str, Any] | None:
    by_id = load_pricing()["by_id"]
    return next((m for mid, m in by_id.items() if mid.split("/", 1)[1] == model), None)


def cost_from_usage(
    usage: list[dict[str, Any]], *, stt_language: str | None = None
) -> dict[str, Any]:
    """Price LiveKit's measured per-model usage (`AgentSession.usage`) for one call.

    LiveKit's STT usage names the model but not the language, so pass `stt_language` (the
    language code the STT ran with) to bill Deepgram's "multi" code-switching mode at its
    multilingual rate. Token-billed transcription (the OpenAI realtime model's input
    transcription) and token-billed TTS are priced by tokens.
    """
    costs: dict[str, float] = {}
    unpriced: list[str] = []
    for u in usage:
        entry = _pricing_entry(u.get("model", ""))
        if entry is None:
            if u.get("type") in ("llm_usage", "stt_usage", "tts_usage"):
                unpriced.append(f"{u.get('provider')}/{u.get('model')}")
            continue
        p = entry["prices"]
        if entry["kind"] == "realtime":
            audio_in, cached_audio = (
                u.get("input_audio_tokens", 0),
                u.get("input_cached_audio_tokens", 0),
            )
            text_in, cached_text = (
                u.get("input_text_tokens", 0),
                u.get("input_cached_text_tokens", 0),
            )
            usd = (
                (audio_in - cached_audio) * p["audio_input"]
                + cached_audio * p["audio_input_cached"]
                + (text_in - cached_text) * p["text_input"]
                + cached_text * p["text_input_cached"]
                + u.get("output_audio_tokens", 0) * p["audio_output"]
                + u.get("output_text_tokens", 0) * p["text_output"]
            ) / 1e6
        elif entry["kind"] == "llm":
            cached = u.get("input_cached_tokens", 0)
            usd = (
                (u.get("input_tokens", 0) - cached) * p["input"]
                + cached * p["cached_input"]
                + u.get("output_tokens", 0) * p["output"]
            ) / 1e6
        elif entry["kind"] == "stt":
            if "input" in p and (u.get("input_tokens") or u.get("output_tokens")):
                usd = (
                    u.get("input_tokens", 0) * p["input"] + u.get("output_tokens", 0) * p["output"]
                ) / 1e6
            else:
                multi = stt_language == "multi" and "per_minute_streaming_multilingual" in p
                rate = p["per_minute_streaming_multilingual" if multi else "per_minute_streaming"]
                usd = u.get("audio_duration", 0.0) / 60 * rate
        elif entry["kind"] == "tts":
            if "per_1m_characters" in p:
                usd = u.get("characters_count", 0) * p["per_1m_characters"] / 1e6
            else:
                usd = (
                    u.get("input_tokens", 0) * p["text_input"]
                    + u.get("output_tokens", 0) * p["audio_output"]
                ) / 1e6
        else:
            continue
        costs[entry["id"]] = costs.get(entry["id"], 0.0) + usd
    return {"by_model": costs, "total_usd": round(sum(costs.values()), 5), "unpriced": unpriced}


def usage_record(
    usage: list[dict[str, Any]],
    *,
    overhead: list[dict[str, Any]],
    stt_language: str | None = None,
) -> dict[str, Any]:
    """`ScenarioResult.usage`: the agent's own usage and cost, and separately what the eval
    itself spent on the call (simulated caller and judge)."""
    return {
        "models": usage,
        "cost": cost_from_usage(usage, stt_language=stt_language),
        "eval_overhead": {"models": overhead, "cost": cost_from_usage(overhead)},
    }


def comparison(p: CallProfile = CallProfile()) -> dict[str, dict[str, float]]:
    return {
        "realtime: gpt-realtime-2 (cache hits)": openai_realtime(p),
        "realtime: gpt-realtime-2 (no cache hits)": openai_realtime(p, cache=False),
        "realtime: gpt-realtime-2.1-mini (cache hits)": openai_realtime(
            p, "openai/gpt-realtime-2.1-mini"
        ),
        "pipeline: nova-3 + gpt-4.1-mini + sonic-3": pipeline(p),
        "pipeline: nova-3 multilingual + gpt-4.1-mini + sonic-3": pipeline(
            p, stt=("deepgram/nova-3", "per_minute_streaming_multilingual")
        ),
    }


def to_markdown(p: CallProfile = CallProfile()) -> str:
    phone_infra = sum(infrastructure(p, phone=True).values())
    lines = [
        f"Call: {p.duration_s:.0f} s, {p.user_turns} turns, {len(p.tool_turns)} tool calls. "
        f"Prices as of {load_pricing()['as_of']} (src/receptionist/evals/data/pricing.yaml).",
        "",
        "| architecture | model cost / call | per minute | + LiveKit Cloud + SIP |",
        "|---|---:|---:|---:|",
    ]
    for name, parts in comparison(p).items():
        total = sum(parts.values())
        lines.append(
            f"| {name} | ${total:.3f} | ${total / p.minutes:.3f} | ${total + phone_infra:.3f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate per-call cost for each architecture.")
    parser.add_argument("--minutes", type=float, default=3.0)
    parser.add_argument("--turns", type=int, default=8)
    args = parser.parse_args()
    scale = args.minutes / 3.0
    turns = args.turns
    profile = replace(
        CallProfile(),
        duration_s=args.minutes * 60,
        user_turns=turns,
        user_speech_s=81 * scale,
        agent_speech_s=72 * scale,
        user_text_tokens=round(269 * scale),
        agent_text_tokens=round(239 * scale),
        agent_characters=round(1026 * scale),
        tool_turns=tuple(range(2, turns + 1, 2))[: max(1, round(3 * scale))],
    )
    print(to_markdown(profile))


if __name__ == "__main__":
    main()
