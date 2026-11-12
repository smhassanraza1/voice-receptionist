"""Runtime configuration for the MCP server, read from the environment (and `.env`)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .rag.retriever import DEFAULT_MIN_CONFIDENCE

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KB_DIR = REPO_ROOT / "data" / "kb"

# Issuer claim every token must carry. Tokens are minted out of band by `mint_token`; there is
# no interactive OAuth flow, so this is a stable name rather than a discoverable URL.
TOKEN_ISSUER = "receptionist-auth"


class Settings(BaseSettings):
    """MCP server settings. Every field maps to an `MCP_*` environment variable."""

    model_config = SettingsConfigDict(
        env_prefix="MCP_", env_file=".env", extra="ignore", populate_by_name=True
    )

    server_url: str = Field(
        default="http://127.0.0.1:8765/mcp",
        description="Public URL of the MCP endpoint; also the JWT audience.",
    )
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    jwt_secret: SecretStr | None = Field(
        default=None,
        description="HS256 key. Required to serve over HTTP; in-process use needs none.",
    )
    token_leeway_seconds: int = Field(default=30, ge=0, le=300)

    db_path: Path = Path("data/clinic.db")
    kb_dir: Path = DEFAULT_KB_DIR

    # Abuse controls.
    rate_limit_per_minute: float = Field(default=120, gt=0)
    rate_limit_burst: int = Field(default=30, ge=1)
    lockout_max_failures: int = Field(default=5, ge=1)
    lockout_window_minutes: float = Field(default=15, gt=0)
    max_request_body_bytes: int = Field(default=64 * 1024, ge=1024)
    allowed_hosts: list[str] = Field(
        default_factory=list,
        description="Extra Host header values to accept (DNS-rebinding protection).",
    )

    # Retrieval.
    rag_dense: Literal["none", "fastembed", "openai"] = Field(
        default="none", validation_alias=AliasChoices("RAG_DENSE", "MCP_RAG_DENSE")
    )
    rag_min_confidence: float = Field(default=DEFAULT_MIN_CONFIDENCE, ge=0, le=1)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("jwt_secret", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        return value or None

    @field_validator("jwt_secret")
    @classmethod
    def _secret_is_long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().encode()) < 32:
            raise ValueError("MCP_JWT_SECRET must be at least 32 bytes")
        return value

    @field_validator("server_url")
    @classmethod
    def _server_url_is_http(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("MCP_SERVER_URL must be an absolute http(s) URL")
        return value

    @property
    def mcp_path(self) -> str:
        """Path the streamable HTTP endpoint is served at (taken from `server_url`)."""
        return urlsplit(self.server_url).path or "/mcp"

    @property
    def issuer_origin(self) -> str:
        """Origin advertised as the authorization server in protected-resource metadata."""
        parts = urlsplit(self.server_url)
        return f"{parts.scheme}://{parts.netloc}"

    def trusted_hosts(self) -> list[str]:
        """Host header values accepted by the transport: loopback, `server_url`, and extras."""
        public = urlsplit(self.server_url).netloc
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", public, *self.allowed_hosts]
        return list(dict.fromkeys(hosts))

    def trusted_origins(self) -> list[str]:
        """Origin header values accepted (only browsers send one)."""
        origins = [f"http://{host}" for host in self.trusted_hosts()] + [self.issuer_origin]
        return list(dict.fromkeys(origins))
