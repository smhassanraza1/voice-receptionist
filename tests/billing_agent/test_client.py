"""BillingAgentClient failure handling: the voice agent must always get a speakable answer."""

from __future__ import annotations

import asyncio
import socket
import time

import httpx

from receptionist.billing_agent.client import BillingAgentClient

from ._support import BASE_URL


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_dead_server_returns_a_fallback() -> None:
    async with BillingAgentClient(
        f"http://127.0.0.1:{_unused_port()}", "token", timeout=2
    ) as client:
        reply = await client.ask("What do I owe?", patient_id="P-1002", verified=True)

    assert reply.text.startswith("I'm sorry, our billing system isn't available right now.")
    assert reply.data == {"state": "unavailable", "error": "AgentCardResolutionError"}
    assert reply.context_id


async def test_fallback_keeps_the_callers_context_and_language() -> None:
    async with BillingAgentClient(
        f"http://127.0.0.1:{_unused_port()}", "token", timeout=2
    ) as client:
        reply = await client.ask(
            "¿Cuánto debo?", patient_id="P-1001", verified=True, language="es", context_id="ctx-1"
        )

    assert reply.context_id == "ctx-1"
    assert reply.text.startswith("Lo siento")


async def test_hung_server_times_out() -> None:
    async def never_answers(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    transport = httpx.MockTransport(never_answers)
    async with BillingAgentClient(BASE_URL, "token", timeout=0.2, transport=transport) as client:
        started = time.monotonic()
        reply = await client.ask("What do I owe?", patient_id="P-1002", verified=True)

    assert time.monotonic() - started < 2
    assert reply.data["state"] == "unavailable"


async def test_wrong_token_returns_a_fallback(transport: httpx.ASGITransport) -> None:
    async with BillingAgentClient(BASE_URL, "not-the-token", transport=transport) as client:
        reply = await client.ask("What do I owe?", patient_id="P-1002", verified=True)

    assert reply.data["state"] == "unavailable"
    assert "185" not in reply.text
