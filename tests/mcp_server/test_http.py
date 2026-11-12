"""End to end: the real ASGI app under uvicorn, reached over HTTP with bearer tokens."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from receptionist.common import clock
from receptionist.mcp_server.auth import KB_READ, mint_token
from receptionist.mcp_server.db import IDENTITY_FAILED_MESSAGE
from receptionist.mcp_server.security import (
    FORBIDDEN_MESSAGE,
    LOCKED_OUT_MESSAGE,
    RATE_LIMITED_MESSAGE,
)
from receptionist.mcp_server.server import build_app

from .conftest import JWT_SECRET, error_text, make_settings, payload


@dataclass(frozen=True)
class LiveServer:
    url: str

    @property
    def base(self) -> str:
        return self.url.removesuffix("/mcp")

    def token(self, subject: str = "voice-agent", **options) -> str:
        return mint_token(JWT_SECRET, subject=subject, audience=self.url, **options)


@contextmanager
def serve(tmp_path: Path, **overrides) -> Iterator[LiveServer]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    settings = make_settings(tmp_path, jwt_secret=JWT_SECRET, server_url=url, **overrides)
    server = uvicorn.Server(uvicorn.Config(build_app(settings), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.01)
    try:
        yield LiveServer(url)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveServer]:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CLINIC_NOW", clock.EVAL_NOW)
        with serve(tmp_path_factory.mktemp("live")) as server:
            yield server


@asynccontextmanager
async def mcp_client(url: str, token: str, mode: str = "legacy") -> AsyncIterator[Client]:
    """Legacy mode is what LiveKit's MCPServerHTTP speaks: a session with an initialize call."""
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(10, read=30)) as http,
        Client(streamable_http_client(url, http_client=http), mode=mode) as client,
    ):
        yield client


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "curl", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def post(live: LiveServer, *, token: str | None = None, **kwargs) -> httpx2.Response:
    headers = dict(MCP_HEADERS) | kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx2.post(live.url, headers=headers, timeout=10, **({"json": INITIALIZE} | kwargs))


# --------------------------------------------------------------------------- HTTP layer


def test_health_check_needs_no_token(live: LiveServer) -> None:
    response = httpx2.get(f"{live.base}/healthz", timeout=10)
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_protected_resource_metadata_names_this_server(live: LiveServer) -> None:
    response = httpx2.get(f"{live.base}/.well-known/oauth-protected-resource/mcp", timeout=10)
    assert response.json()["resource"] == live.url


def test_valid_token_is_accepted(live: LiveServer) -> None:
    assert post(live, token=live.token()).status_code == 200


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(None, id="missing"),
        pytest.param("garbage", id="garbage"),
        pytest.param("wrong-secret", id="bad-signature"),
        pytest.param("expired", id="expired"),
        pytest.param("other-audience", id="wrong-audience"),
    ],
)
def test_requests_without_a_valid_token_get_401(live: LiveServer, token: str | None) -> None:
    tokens = {
        "wrong-secret": mint_token("z" * 40, subject="voice-agent", audience=live.url),
        "expired": live.token(ttl=-timedelta(minutes=5)),
        "other-audience": mint_token(JWT_SECRET, subject="x", audience="http://elsewhere/mcp"),
    }
    response = post(live, token=tokens.get(token, token))
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_foreign_host_headers_are_rejected(live: LiveServer) -> None:
    response = post(live, token=live.token(), headers={"Host": "attacker.example"})
    assert response.status_code == 421


def test_oversized_bodies_are_rejected(live: LiveServer) -> None:
    body = {**INITIALIZE, "params": {**INITIALIZE["params"], "padding": "x" * 100_000}}
    assert post(live, token=live.token(), json=body).status_code == 413


# --------------------------------------------------------------------------- MCP over HTTP


async def test_booking_flow_over_http(live: LiveServer) -> None:
    async with mcp_client(live.url, live.token()) as client:
        kb = payload(await client.call_tool("search_knowledge_base", {"query": "parking garage"}))
        assert kb["results"][0]["doc_id"] == "parking-directions"

        slots = payload(await client.call_tool("find_available_slots", {"specialty": "cardiology"}))
        booking = payload(
            await client.call_tool(
                "book_appointment",
                {
                    "slot_id": slots["slots"][0]["slot_id"],
                    "patient_name": "Aisha Rahman",
                    "date_of_birth": "1999-07-21",
                    "phone": "555-010-1003",
                    "reason": "palpitations",
                },
            )
        )
        assert (booking["patient_id"], booking["identity_verified"]) == ("P-1003", True)

        lookup = payload(
            await client.call_tool(
                "lookup_appointments",
                {"date_of_birth": "1999-07-21", "confirmation_code": booking["confirmation_code"]},
            )
        )
        assert [a["specialty"] for a in lookup["appointments"]] == ["cardiology"]

        cancelled = payload(
            await client.call_tool(
                "cancel_appointment",
                {"confirmation_code": booking["confirmation_code"], "date_of_birth": "1999-07-21"},
            )
        )
        assert cancelled["status"] == "cancelled"


async def test_modern_protocol_clients_are_served(live: LiveServer) -> None:
    async with mcp_client(live.url, live.token(), mode="auto") as client:
        providers = payload(await client.call_tool("list_providers", {"language": "fr"}))
    assert [p["name"] for p in providers["providers"]] == ["Alex Moreau, NP"]


async def test_scopes_are_enforced_per_tool(live: LiveServer) -> None:
    async with mcp_client(live.url, live.token("kiosk", scopes=[KB_READ])) as client:
        payload(await client.call_tool("search_knowledge_base", {"query": "flu shots"}))
        denied = await client.call_tool(
            "lookup_appointments", {"date_of_birth": "1985-03-14", "confirmation_code": "LKC-7Q2M"}
        )
    assert error_text(denied).endswith(FORBIDDEN_MESSAGE)


async def test_rate_limit_is_per_client(tmp_path: Path) -> None:
    with serve(tmp_path, rate_limit_burst=3, rate_limit_per_minute=1) as live:
        async with mcp_client(live.url, live.token("noisy")) as noisy:
            for _ in range(3):
                payload(await noisy.call_tool("list_providers", {}))
            limited = await noisy.call_tool("list_providers", {})
        async with mcp_client(live.url, live.token("quiet")) as quiet:
            payload(await quiet.call_tool("list_providers", {}))
    assert error_text(limited).endswith(RATE_LIMITED_MESSAGE)


async def test_identity_lockout_is_per_call_session(tmp_path: Path) -> None:
    wrong = {"date_of_birth": "1985-03-15", "confirmation_code": "LKC-7Q2M"}
    right = {"date_of_birth": "1985-03-14", "confirmation_code": "LKC-7Q2M"}
    with serve(tmp_path, lockout_max_failures=2) as live:
        token = live.token()
        async with mcp_client(live.url, token) as first_call:
            for _ in range(2):
                failed = await first_call.call_tool("lookup_appointments", wrong)
                assert error_text(failed).endswith(IDENTITY_FAILED_MESSAGE)
            locked = await first_call.call_tool("lookup_appointments", right)
            assert error_text(locked).endswith(LOCKED_OUT_MESSAGE)
        # The voice agent opens one MCP session per phone call; the next caller is unaffected.
        async with mcp_client(live.url, token) as next_call:
            payload(await next_call.call_tool("lookup_appointments", right))
