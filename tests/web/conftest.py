"""Fixtures for the web/token server tests. No LiveKit server is needed: tokens are just JWTs."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
import uvicorn
from livekit import api
from starlette.testclient import TestClient

from receptionist.web import WebSettings, create_app

LIVEKIT_URL = "wss://lakeside-test.livekit.cloud"
API_KEY = "APIlakesidetest"
API_SECRET = "lakeside-test-secret-0123456789abcdef0123456789"
INTERNAL_TOKEN = "internal-test-token-6f1c"
OPERATOR_PASSCODE = "harbor-street-200"

_MANAGED_ENV = (
    "AGENT_NAME",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "INTERNAL_API_TOKEN",
    "OPERATOR_PASSCODE",
    "WEB_HOST",
    "WEB_PORT",
    "WEB_STATIC_DIR",
    "WEB_TOKEN_TTL_SECONDS",
    "WEB_TOKEN_RATE_LIMIT",
    "WEB_TOKEN_RATE_WINDOW_SECONDS",
    "WEB_AUTH_FAILURE_LIMIT",
    "WEB_AUTH_FAILURE_WINDOW_SECONDS",
)


@pytest.fixture
def web_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Dummy credentials in the environment; a developer's real `.env` is never read."""
    monkeypatch.chdir(tmp_path)
    for name in _MANAGED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIVEKIT_URL", LIVEKIT_URL)
    monkeypatch.setenv("LIVEKIT_API_KEY", API_KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", API_SECRET)
    monkeypatch.setenv("INTERNAL_API_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("OPERATOR_PASSCODE", OPERATOR_PASSCODE)
    monkeypatch.setenv("WEB_STATIC_DIR", str(tmp_path / "dist"))  # not built unless a test says so
    return monkeypatch


@pytest.fixture
def make_client(web_env: pytest.MonkeyPatch) -> Iterator[Callable[[], TestClient]]:
    """Build a TestClient after the test has adjusted the environment."""
    clients: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(create_app(WebSettings(_env_file=None)))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[[], TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": INTERNAL_TOKEN}


@pytest.fixture
def operator_headers() -> dict[str, str]:
    return {"X-Operator-Passcode": OPERATOR_PASSCODE}


@pytest.fixture
def live_server(web_env: pytest.MonkeyPatch) -> Iterator[str]:
    """The app on a real socket, for streaming (SSE) tests TestClient can't do."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        create_app(WebSettings(_env_file=None)),
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.fixture
def verify_token() -> Callable[[str], api.access_token.Claims]:
    """Verify a minted JWT with the dummy secret and parse it the way LiveKit server does."""
    verifier = api.TokenVerifier(API_KEY, API_SECRET)
    return verifier.verify


@pytest.fixture
def raw_claims() -> Callable[[str], dict[str, Any]]:
    def decode(token: str) -> dict[str, Any]:
        return jwt.decode(token, API_SECRET, algorithms=["HS256"], issuer=API_KEY)

    return decode


@pytest.fixture
def livekit_url() -> str:
    return LIVEKIT_URL


@pytest.fixture
def operator_passcode() -> str:
    return OPERATOR_PASSCODE
