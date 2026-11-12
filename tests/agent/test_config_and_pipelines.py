import json

import pytest

from receptionist.agent.pipelines import build_components
from receptionist.agent.prompts import receptionist_instructions
from receptionist.agent.settings import AgentSettings, CallConfig


@pytest.fixture
def settings() -> AgentSettings:
    return AgentSettings(_env_file=None)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (None, ("realtime", "auto")),
        (json.dumps({"mode": "pipeline", "language": "es"}), ("pipeline", "es")),
        # untrusted metadata can't select anything outside the allowed set
        (json.dumps({"mode": "admin", "language": "xx"}), ("realtime", "auto")),
        ("not json", ("realtime", "auto")),
        ("[1, 2]", ("realtime", "auto")),
    ],
)
def test_call_config_from_metadata(settings, metadata, expected):
    config = CallConfig.from_metadata(metadata, settings)
    assert (config.mode, config.language) == expected


def test_openai_realtime_uses_server_semantic_vad(settings):
    c = build_components(CallConfig("realtime", "auto"), settings)
    assert c.labels["llm"] == "openai/gpt-realtime-2"
    assert c.turn_handling["turn_detection"] == "realtime_llm"
    assert c.stt is None and c.tts is None
    assert c.llm._opts.turn_detection.type == "semantic_vad"


def test_realtime_with_livekit_turns_disables_server_vad(settings):
    s = settings.model_copy(update={"realtime_turn_detection": "livekit"})
    c = build_components(CallConfig("realtime", "auto"), s)
    assert c.llm._opts.turn_detection is None
    assert c.turn_handling["interruption"]["mode"] == "adaptive"
    assert "LiveKit turn detector" in c.labels["turns"]


def test_openai_transcript_language_is_pinned_when_chosen(settings):
    auto = build_components(CallConfig("realtime", "auto"), settings)
    english = build_components(CallConfig("realtime", "en"), settings)
    assert auto.llm._opts.input_audio_transcription.language is None
    assert english.llm._opts.input_audio_transcription.language == "en"


@pytest.mark.parametrize(
    ("language", "stt_lang", "tts_lang"), [("auto", "multi", "en"), ("fr", "fr", "fr")]
)
def test_pipeline_components(settings, language, stt_lang, tts_lang):
    c = build_components(CallConfig("pipeline", language), settings)
    assert c.labels["stt"] == f"deepgram/nova-3:{stt_lang}"
    assert c.tts._opts.language == tts_lang
    assert c.turn_handling["preemptive_generation"]["enabled"] is True
    kwargs = c.session_kwargs()
    assert {"stt", "llm", "tts", "turn_handling", "tts_text_transforms"} <= kwargs.keys()


def test_elevenlabs_option(settings):
    s = settings.model_copy(update={"tts_provider": "elevenlabs"})
    c = build_components(CallConfig("pipeline", "auto"), s)
    assert c.labels["tts"] == "elevenlabs/eleven_flash_v2_5"


def test_instructions_carry_date_language_and_safety_rules():
    text = receptionist_instructions("ja")
    assert "November 2, 2026" in text
    assert "Japanese" in text
    assert "911" in text and "medical advice" in text
    assert "Start in English" in receptionist_instructions("auto")
