"""The architectures under comparison, by name."""

from __future__ import annotations

from typing import Literal

from livekit.agents import llm

from receptionist.agent.settings import AgentSettings, CallConfig

ArchName = Literal["pipeline", "realtime"]
ARCHITECTURES: tuple[ArchName, ...] = ("pipeline", "realtime")


def call_config(arch: ArchName, language: str = "auto") -> CallConfig:
    return CallConfig(mode="pipeline" if arch == "pipeline" else "realtime", language=language)


def text_model(arch: ArchName, settings: AgentSettings) -> llm.LLM | llm.RealtimeModel:
    """The "brain" of each architecture, run with text in and text out.

    For realtime models this isolates reasoning and tool use from the audio path; the voice
    channel (`voice.runner`) measures the full speech-to-speech behaviour.
    """
    if arch == "pipeline":
        from livekit.plugins import openai

        return openai.LLM(model=settings.llm_model)
    from livekit.plugins import openai
    from openai.types.realtime import RealtimeReasoning

    return openai.realtime.RealtimeModel(
        model=settings.openai_realtime_model,
        modalities=["text"],
        reasoning=RealtimeReasoning(effort=settings.openai_reasoning_effort),
    )


def model_label(arch: ArchName, settings: AgentSettings) -> str:
    return {
        "pipeline": f"deepgram/{settings.stt_model} + openai/{settings.llm_model} + {settings.tts_provider}",
        "realtime": f"openai/{settings.openai_realtime_model}",
    }[arch]
