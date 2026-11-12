"""Configuration for the web/token server, read from the environment (and `.env`)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/receptionist/web/settings.py -> repository root -> web/dist
DEFAULT_STATIC_DIR = Path(__file__).resolve().parents[3] / "web" / "dist"


class WebSettings(BaseSettings):
    """Settings for `python -m receptionist.web`.

    Field names map to upper-case environment variables (`livekit_url` <- `LIVEKIT_URL`).
    Secrets left empty disable the feature that needs them instead of failing at startup, so the
    server is still useful for frontend work without a LiveKit project.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The worker name caller tokens dispatch; the voice agent registers under the same `AGENT_NAME`.
    agent_name: str = Field(default="receptionist", min_length=1)

    # LiveKit project credentials (shared with the voice agent).
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: SecretStr = SecretStr("")

    # Shared secret the voice agent sends as `X-Internal-Token` when it escalates a call.
    internal_api_token: SecretStr = SecretStr("")
    # Passcode for the operator console (`X-Operator-Passcode`).
    operator_passcode: SecretStr = SecretStr("")

    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8000, ge=1, le=65535)
    web_static_dir: Path = DEFAULT_STATIC_DIR

    # Browser participant tokens are short-lived. LiveKit checks expiry only when a participant
    # joins (the server refreshes tokens for connected participants), so this bounds how long a
    # minted token can be used to join, not how long a call can last.
    web_token_ttl_seconds: int = Field(default=15 * 60, ge=60, le=6 * 60 * 60)
    # Per-IP limits. Each browser call mints ~2 tokens (one pre-fetched, one after hang-up).
    web_token_rate_limit: int = Field(default=20, ge=1)
    web_token_rate_window_seconds: float = Field(default=60.0, gt=0)
    web_auth_failure_limit: int = Field(default=10, ge=1)
    web_auth_failure_window_seconds: float = Field(default=300.0, gt=0)

    @property
    def livekit_configured(self) -> bool:
        return bool(
            self.livekit_url and self.livekit_api_key and self.livekit_api_secret.get_secret_value()
        )

    @property
    def internal_token(self) -> str | None:
        return self.internal_api_token.get_secret_value().strip() or None

    @property
    def operator_secret(self) -> str | None:
        return self.operator_passcode.get_secret_value().strip() or None
