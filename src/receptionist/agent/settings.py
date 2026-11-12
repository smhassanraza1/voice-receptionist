"""Voice agent configuration.

Process-wide defaults come from the environment; each call can override the architecture
and language through LiveKit dispatch metadata (see `CallConfig`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("receptionist.agent")

Mode = Literal["realtime", "pipeline"]

SUPPORTED_LANGUAGES = ("auto", "en", "es", "fr", "de", "hi", "ja")


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    agent_name: str = "receptionist"

    mode: Mode = Field(default="realtime", validation_alias="RECEPTIONIST_MODE")
    # "server": the realtime model's own semantic_vad decides turns and barge-in. "livekit":
    # LiveKit's turn detector + adaptive interruption run locally in front of the realtime model.
    realtime_turn_detection: Literal["server", "livekit"] = Field(
        default="server", validation_alias="RECEPTIONIST_REALTIME_TURN_DETECTION"
    )
    # "adaptive" uses LiveKit's model to tell real barge-in from backchannels ("mm-hmm");
    # "vad" interrupts on any detected speech longer than `interruption.min_duration`.
    interruption_mode: Literal["adaptive", "vad"] = Field(
        default="adaptive", validation_alias="RECEPTIONIST_INTERRUPTION_MODE"
    )

    # Speech-to-speech models
    openai_realtime_model: str = "gpt-realtime-2"
    openai_realtime_voice: str = "marin"
    openai_reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"

    # Cascaded pipeline
    stt_model: str = Field(default="nova-3", validation_alias="RECEPTIONIST_STT_MODEL")
    llm_model: str = Field(default="gpt-4.1-mini", validation_alias="RECEPTIONIST_LLM_MODEL")
    tts_provider: Literal["cartesia", "elevenlabs"] = Field(
        default="cartesia", validation_alias="RECEPTIONIST_TTS_PROVIDER"
    )
    cartesia_model: str = "sonic-3"
    cartesia_voice: str = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_voice: str = "21m00Tcm4TlvDq8ikWAM"

    # Tools and integrations
    mcp_server_url: str = "http://127.0.0.1:8765/mcp"
    mcp_auth_token: str = ""
    billing_agent_url: str = "http://127.0.0.1:8766"
    billing_agent_token: str = ""
    web_base_url: str = "http://127.0.0.1:8000"
    internal_api_token: str = ""

    # Human handoff over SIP: warm transfer needs an outbound trunk, cold transfer only
    # needs SIP REFER enabled on the inbound trunk.
    human_transfer_number: str = ""
    sip_outbound_trunk: str = Field(default="", validation_alias="LIVEKIT_SIP_OUTBOUND_TRUNK")
    human_transfer_mode: Literal["warm", "cold"] = "cold"

    # Hard cap on a call's length, after which the agent says goodbye and hangs up; 0 = no cap.
    max_call_seconds: float = Field(
        default=600.0, ge=0, validation_alias="RECEPTIONIST_MAX_CALL_SECONDS"
    )


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()


@dataclass(frozen=True)
class CallConfig:
    """Per-call options, resolved from dispatch metadata on top of process defaults."""

    mode: Mode
    language: str  # "auto" or ISO 639-1

    @classmethod
    def from_metadata(cls, raw: str | None, settings: AgentSettings) -> CallConfig:
        data: dict = {}
        if raw:
            try:
                parsed = json.loads(raw)
                data = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                logger.warning("ignoring malformed job metadata")

        mode = data.get("mode", settings.mode)
        language = data.get("language", "auto")
        return cls(
            mode=mode if mode in ("realtime", "pipeline") else settings.mode,
            language=language if language in SUPPORTED_LANGUAGES else "auto",
        )
