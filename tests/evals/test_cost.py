"""The cost model must reproduce hand-checked figures for the default 3-minute call."""

import pytest

from receptionist.evals import cost


def total(parts: dict[str, float]) -> float:
    return sum(parts.values())


@pytest.mark.parametrize(
    ("fn", "kwargs", "expected"),
    [
        (cost.openai_realtime, {}, 0.188),
        (cost.openai_realtime, {"cache": False}, 0.562),
        (cost.openai_realtime, {"model_id": "openai/gpt-realtime-2.1-mini"}, 0.060),
        (cost.pipeline, {}, 0.0585),
        (cost.pipeline, {"stt": ("deepgram/nova-3", "per_minute_streaming_multilingual")}, 0.0615),
    ],
)
def test_worked_example(fn, kwargs, expected):
    assert total(fn(cost.CallProfile(), **kwargs)) == pytest.approx(expected, abs=0.0006)


def test_openai_token_accounting():
    # 2,370 uncached text tokens and 19,350 cached ones at $4 / $0.40 per 1M
    parts = cost.openai_realtime(cost.CallProfile())
    assert parts["text_in"] == pytest.approx(2370 * 4.0 / 1e6)
    assert parts["text_in_cached"] == pytest.approx(19350 * 0.4 / 1e6)
    assert parts["audio_in"] == pytest.approx(2110 * 32.0 / 1e6)


def test_realtime_cost_grows_faster_than_call_length():
    short = cost.CallProfile()
    long = cost.CallProfile(
        duration_s=600,
        user_turns=26,
        user_speech_s=270,
        agent_speech_s=240,
        user_text_tokens=897,
        agent_text_tokens=797,
        agent_characters=3420,
        tool_turns=(2, 6, 10, 14, 18, 22),
    )
    per_min = lambda fn, p, **kw: total(fn(p, **kw)) / p.minutes  # noqa: E731
    # every pass re-sends the conversation: without cache hits that compounds, with them it doesn't
    realtime = cost.openai_realtime
    assert per_min(realtime, long, cache=False) > 1.8 * per_min(realtime, short, cache=False)
    assert per_min(realtime, long) == pytest.approx(per_min(realtime, short), rel=0.15)
    assert per_min(cost.pipeline, long) == pytest.approx(per_min(cost.pipeline, short), rel=0.15)


def test_markdown_lists_every_architecture():
    md = cost.to_markdown()
    assert md.count("| realtime:") == 3 and md.count("| pipeline:") == 2


def test_cost_from_measured_usage():
    usage = [
        {
            "type": "llm_usage",
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "input_tokens": 10_000,
            "input_cached_tokens": 8_000,
            "output_tokens": 500,
        },
        {"type": "stt_usage", "provider": "deepgram", "model": "nova-3", "audio_duration": 180.0},
        {"type": "tts_usage", "provider": "cartesia", "model": "sonic-3", "characters_count": 1000},
        {"type": "llm_usage", "provider": "acme", "model": "unknown-1"},
        {"type": "eot_usage", "provider": "livekit", "model": "turn-detector-v1"},
    ]
    out = cost.cost_from_usage(usage)
    assert out["by_model"]["openai/gpt-4.1-mini"] == pytest.approx(
        (2000 * 0.4 + 8000 * 0.1 + 500 * 1.6) / 1e6
    )
    assert out["by_model"]["deepgram/nova-3"] == pytest.approx(3 * 0.0048)
    assert out["by_model"]["cartesia/sonic-3"] == pytest.approx(1000 * 39.2 / 1e6)
    assert out["unpriced"] == ["acme/unknown-1"]


def test_realtime_usage_splits_cached_audio_and_text():
    usage = [
        {
            "type": "llm_usage",
            "provider": "openai",
            "model": "gpt-realtime-2",
            "input_audio_tokens": 1000,
            "input_cached_audio_tokens": 400,
            "input_text_tokens": 2000,
            "input_cached_text_tokens": 1500,
            "output_audio_tokens": 300,
            "output_text_tokens": 50,
        }
    ]
    expected = (600 * 32 + 400 * 0.4 + 500 * 4 + 1500 * 0.4 + 300 * 64 + 50 * 24) / 1e6
    assert cost.cost_from_usage(usage)["total_usd"] == pytest.approx(expected, abs=1e-5)


def test_multilingual_stt_is_billed_at_the_multilingual_rate():
    usage = [{"type": "stt_usage", "provider": "Deepgram", "model": "nova-3", "audio_duration": 60}]
    assert cost.cost_from_usage(usage)["total_usd"] == pytest.approx(0.0048)
    assert cost.cost_from_usage(usage, stt_language="en-US")["total_usd"] == pytest.approx(0.0048)
    assert cost.cost_from_usage(usage, stt_language="multi")["total_usd"] == pytest.approx(0.0058)


def test_realtime_input_transcription_is_priced_by_tokens():
    # LiveKit reports the realtime model's input transcription as tokens, with no audio duration
    usage = [
        {
            "type": "stt_usage",
            "provider": "api.openai.com",
            "model": "gpt-4o-mini-transcribe",
            "input_tokens": 2000,
            "input_audio_tokens": 1900,
            "output_tokens": 300,
            "audio_duration": 0.0,
        }
    ]
    expected = (2000 * 1.25 + 300 * 5.0) / 1e6
    assert cost.cost_from_usage(usage)["total_usd"] == pytest.approx(expected, abs=1e-5)


def test_token_billed_tts_is_priced_by_tokens():
    usage = [
        {
            "type": "tts_usage",
            "provider": "api.openai.com",
            "model": "gpt-4o-mini-tts",
            "input_tokens": 1000,
            "output_tokens": 20_000,
        }
    ]
    expected = (1000 * 0.60 + 20_000 * 12.0) / 1e6
    assert cost.cost_from_usage(usage)["total_usd"] == pytest.approx(expected, abs=1e-5)


def test_usage_record_keeps_eval_overhead_separate():
    agent = [{"type": "llm_usage", "model": "gpt-4.1-mini", "input_tokens": 1_000_000}]
    judge = [{"type": "llm_usage", "model": "gpt-5-mini", "output_tokens": 1_000_000}]
    record = cost.usage_record(agent, overhead=judge)
    assert record["cost"]["total_usd"] == pytest.approx(0.40)
    assert record["eval_overhead"]["cost"]["total_usd"] == pytest.approx(2.00)
