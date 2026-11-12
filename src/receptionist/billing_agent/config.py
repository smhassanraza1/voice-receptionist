"""Billing agent settings, read from the environment (and `.env`)."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A non-reasoning model keeps time-to-first-token low enough for a live call (the caller is
# waiting on this answer). Reasoning tiers such as gpt-6-luna work too: set
# BILLING_AGENT_MODEL and BILLING_AGENT_REASONING_EFFORT=low.
DEFAULT_MODEL = "gpt-4.1-mini"


class BillingAgentSettings(BaseSettings):
    """Settings for `python -m receptionist.billing_agent`.

    Every field reads `BILLING_AGENT_<NAME>`, except the OpenAI key, which uses the standard
    `OPENAI_API_KEY`.
    """

    model_config = SettingsConfigDict(
        env_prefix="BILLING_AGENT_", env_file=".env", extra="ignore", populate_by_name=True
    )

    token: SecretStr = Field(min_length=16, description="Shared bearer token callers must present.")
    host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1, le=65535)
    url: str | None = Field(
        default=None,
        description="Public base URL advertised in the agent card (BILLING_AGENT_URL). "
        "Defaults to http://<host>:<port>.",
    )

    llm: Literal["auto", "off"] = Field(
        default="auto",
        description="'auto' uses the OpenAI tool-calling engine when OPENAI_API_KEY is set; "
        "'off' always uses the deterministic rule-based engine.",
    )
    model: str = DEFAULT_MODEL
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = Field(
        default=None,
        description="Only for reasoning models; keep it low for voice latency.",
    )
    llm_timeout_s: float = Field(default=6.0, gt=0)
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY")
    )

    session_ttl_s: float = Field(default=30 * 60, gt=0)
    max_sessions: int = Field(default=1_000, ge=1)
    max_history_turns: int = Field(default=8, ge=1)

    @field_validator("reasoning_effort", "openai_api_key", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        return value or None

    @property
    def public_url(self) -> str:
        base = self.url or f"http://{self.host}:{self.port}"
        return base.rstrip("/") + "/"
