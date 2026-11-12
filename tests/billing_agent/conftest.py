from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.applications import Starlette

from receptionist.billing_agent.client import BillingAgentClient
from receptionist.billing_agent.config import BillingAgentSettings
from receptionist.billing_agent.server import build_app
from receptionist.common.clock import EVAL_NOW

from ._support import BASE_URL, TOKEN


@pytest.fixture(autouse=True)
def _offline_and_pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLINIC_NOW", EVAL_NOW)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BILLING_AGENT_LLM", "off")


@pytest.fixture
def settings() -> BillingAgentSettings:
    return BillingAgentSettings(token=TOKEN, llm="off", _env_file=None)


@pytest.fixture
async def app(settings: BillingAgentSettings) -> AsyncIterator[Starlette]:
    app = build_app(settings)
    yield app
    # httpx.ASGITransport doesn't run the lifespan, so drain the A2A handler here.
    await app.state.request_handler.aclose()


@pytest.fixture
def transport(app: Starlette) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


@pytest.fixture
async def http(transport: httpx.ASGITransport) -> AsyncIterator[httpx.AsyncClient]:
    """Raw HTTP access to the in-process app, without credentials."""
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


@pytest.fixture
async def billing(transport: httpx.ASGITransport) -> AsyncIterator[BillingAgentClient]:
    async with BillingAgentClient(BASE_URL, TOKEN, timeout=5, transport=transport) as client:
        yield client
