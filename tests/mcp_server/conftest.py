from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult

from receptionist.common import clock
from receptionist.mcp_server.db import ClinicStore
from receptionist.mcp_server.server import build_server
from receptionist.mcp_server.settings import Settings

JWT_SECRET = "test-only-secret-" + "x" * 32


@pytest.fixture(autouse=True)
def eval_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs at clock.EVAL_NOW (Monday 2026-11-02 09:00, America/New_York)."""
    monkeypatch.setenv("CLINIC_NOW", clock.EVAL_NOW)
    monkeypatch.delenv("CLINIC_TIMEZONE", raising=False)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"db_path": tmp_path / "clinic.db", "jwt_secret": None} | overrides
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def store(settings: Settings) -> ClinicStore:
    return ClinicStore(settings.db_path)


def connect(settings: Settings) -> Client:
    """In-process client. Enter it inside the test: pytest-asyncio tears async fixtures down in
    another task, which anyio's cancel scopes (used by `Client`) do not allow."""
    return Client(build_server(settings), raise_exceptions=True)


def error_text(result: CallToolResult) -> str:
    assert result.is_error, f"expected a tool error, got {result.structured_content}"
    return " ".join(getattr(part, "text", "") for part in result.content)


def payload(result: CallToolResult) -> dict[str, Any]:
    """Structured result; also checks the text content (what the LLM reads) carries the same JSON."""
    assert not result.is_error, error_text(result)
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content
