"""Builds the two architectures behind a single switch.

- realtime: one speech-to-speech model (OpenAI gpt-realtime-2) hears the caller's audio and
  speaks back directly.
- pipeline: Deepgram STT -> text LLM -> Cartesia/ElevenLabs TTS, with LiveKit's turn detector
  and adaptive interruption deciding when the caller is done or barging in.

Both return keyword arguments for `AgentSession`, so the rest of the agent is identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from livekit.agents import inference, llm, stt, tts
from livekit.agents.voice.transcription.text_transforms import TextTransforms
from livekit.agents.voice.turn import TurnHandlingOptions

from .settings import AgentSettings, CallConfig
from .speech import spell_confirmation_codes

# Deepgram Nova-3: "multi" enables code-switching across en/es/fr/de/hi/ja (and others).
DEEPGRAM_LANGUAGES = {
    "auto": "multi",
    "en": "en-US",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "hi": "hi",
    "ja": "ja",
}


@dataclass
class SessionComponents:
    llm: llm.LLM | llm.RealtimeModel
    stt: stt.STT | None = None
    tts: tts.TTS | None = None
    turn_handling: TurnHandlingOptions = field(default_factory=dict)  # type: ignore[assignment]
    tts_text_transforms: list[TextTransforms] | None = None
    labels: dict[str, str] = field(default_factory=dict)

    def session_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"llm": self.llm, "turn_handling": self.turn_handling}
        if self.stt is not None:
            kwargs["stt"] = self.stt
        if self.tts is not None:
            kwargs["tts"] = self.tts
        if self.tts_text_transforms is not None:
            kwargs["tts_text_transforms"] = self.tts_text_transforms
        # VAD is left to the session default (bundled Silero); with server-side turn detection
        # it only drives user-state events, not turn ends
        return kwargs


def _livekit_turn_handling(settings: AgentSettings) -> TurnHandlingOptions:
    return {
        "turn_detection": inference.TurnDetector(),
        "endpointing": {"mode": "dynamic", "min_delay": 0.4, "max_delay": 3.0},
        "interruption": {
            "mode": settings.interruption_mode,
            "min_duration": 0.4,
            "resume_false_interruption": True,
            "false_interruption_timeout": 1.5,
        },
        "preemptive_generation": {"enabled": True},
    }


def build_realtime(config: CallConfig, settings: AgentSettings) -> SessionComponents:
    server_turns = settings.realtime_turn_detection == "server"

    from livekit.plugins import openai
    from openai.types.realtime import AudioTranscription, RealtimeReasoning
    from openai.types.realtime.realtime_audio_input_turn_detection import SemanticVad

    openai_kwargs: dict[str, Any] = {}
    if config.language != "auto":
        # The caller transcript otherwise guesses the language per utterance, and a short
        # reply like a name can come back in the wrong language and script.
        openai_kwargs["input_audio_transcription"] = AudioTranscription(
            model="gpt-4o-mini-transcribe", language=config.language
        )
    model: llm.RealtimeModel = openai.realtime.RealtimeModel(
        model=settings.openai_realtime_model,
        voice=settings.openai_realtime_voice,
        reasoning=RealtimeReasoning(effort=settings.openai_reasoning_effort),
        # semantic_vad ends the turn on meaning, not only silence; interrupt_response
        # cancels and truncates the reply when the caller barges in.
        turn_detection=(
            SemanticVad(
                type="semantic_vad",
                eagerness="auto",
                create_response=True,
                interrupt_response=True,
            )
            if server_turns
            else None
        ),
        **openai_kwargs,
    )
    label = f"openai/{settings.openai_realtime_model}"

    if server_turns:
        turn_handling: TurnHandlingOptions = {"turn_detection": "realtime_llm"}
        turn_label = "server VAD"
    else:
        turn_handling = _livekit_turn_handling(settings)
        turn_label = f"LiveKit turn detector + {settings.interruption_mode} interruption"

    return SessionComponents(
        llm=model,
        turn_handling=turn_handling,
        labels={"llm": label, "stt": "built-in", "tts": "built-in", "turns": turn_label},
    )


def build_pipeline(config: CallConfig, settings: AgentSettings) -> SessionComponents:
    from livekit.plugins import cartesia, deepgram, elevenlabs, openai

    stt_language = DEEPGRAM_LANGUAGES.get(config.language, "multi")
    stt_engine = deepgram.STT(model=settings.stt_model, language=stt_language, smart_format=True)

    tts_language = config.language if config.language != "auto" else "en"
    tts_engine: tts.TTS
    if settings.tts_provider == "elevenlabs":
        tts_engine = elevenlabs.TTS(
            model=settings.elevenlabs_model,
            voice_id=settings.elevenlabs_voice,
            language=tts_language,
        )
        tts_label = f"elevenlabs/{settings.elevenlabs_model}"
    else:
        tts_engine = cartesia.TTS(
            model=settings.cartesia_model, voice=settings.cartesia_voice, language=tts_language
        )
        tts_label = f"cartesia/{settings.cartesia_model}"

    return SessionComponents(
        llm=openai.LLM(model=settings.llm_model),
        stt=stt_engine,
        tts=tts_engine,
        turn_handling=_livekit_turn_handling(settings),
        tts_text_transforms=["filter_markdown", "filter_emoji", spell_confirmation_codes],
        labels={
            "llm": f"openai/{settings.llm_model}",
            "stt": f"deepgram/{settings.stt_model}:{stt_language}",
            "tts": tts_label,
            "turns": f"LiveKit turn detector + {settings.interruption_mode} interruption",
        },
    )


def build_components(config: CallConfig, settings: AgentSettings) -> SessionComponents:
    if config.mode == "realtime":
        return build_realtime(config, settings)
    return build_pipeline(config, settings)
