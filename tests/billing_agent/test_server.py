"""The A2A surface: agent card, auth, task lifecycle and streaming, over the real ASGI app."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.helpers import get_message_text, new_data_part
from a2a.types import GetTaskRequest, Message, Part, Role, SendMessageRequest, TaskState
from starlette.applications import Starlette

from ._support import BASE_URL, TOKEN

CARD_PATH = "/.well-known/agent-card.json"


def _rpc(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            }
        },
    }


async def test_agent_card_is_public_and_describes_the_agent(http: httpx.AsyncClient) -> None:
    response = await http.get(CARD_PATH)

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Lakeside Family Clinic Billing Agent"
    assert card["version"]
    assert card["capabilities"]["streaming"] is True
    assert card["defaultInputModes"] == ["text/plain", "application/json"]
    (skill,) = card["skills"]
    assert skill["id"] == "billing_inquiry"
    assert skill["examples"]
    (interface,) = card["supportedInterfaces"]
    assert interface == {
        "url": "http://127.0.0.1:8766/",
        "protocolBinding": "JSONRPC",
        "protocolVersion": "1.0",
    }
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert card["securityRequirements"] == [{"schemes": {"bearer": {}}}]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}x"},
    ],
    ids=["missing", "wrong-token", "wrong-scheme", "token-prefix"],
)
async def test_rpc_requires_the_bearer_token(http: httpx.AsyncClient, headers: dict) -> None:
    response = await http.post("/", json=_rpc("What do I owe?"), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_rpc_accepts_the_bearer_token(http: httpx.AsyncClient) -> None:
    response = await http.post(
        "/",
        json=_rpc("Which insurance do you accept?"),
        headers={"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"},
    )

    assert response.status_code == 200
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"


MakeClient = Callable[..., Awaitable[Client]]


@pytest.fixture
async def sdk_client(transport: httpx.ASGITransport) -> AsyncIterator[MakeClient]:
    """The plain SDK client, to observe raw A2A events."""
    clients: list[Client] = []

    async def make(*, streaming: bool) -> Client:
        http = httpx.AsyncClient(transport=transport, headers={"Authorization": f"Bearer {TOKEN}"})
        card = await A2ACardResolver(http, BASE_URL).get_agent_card()
        client = ClientFactory(ClientConfig(httpx_client=http, streaming=streaming)).create(card)
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


def _message(text: str, **data: object) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(
            role=Role.ROLE_USER,
            message_id=uuid.uuid4().hex,
            parts=[Part(text=text), new_data_part(data)],
        )
    )


async def test_streaming_reports_working_then_completed(sdk_client: MakeClient) -> None:
    client = await sdk_client(streaming=True)
    request = _message("What's my balance?", patient_id="P-1002", verified=True)

    events = [event async for event in client.send_message(request)]

    assert events[0].task.status.state == TaskState.TASK_STATE_SUBMITTED
    states = [e.status_update.status.state for e in events if e.HasField("status_update")]
    assert states == [TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_COMPLETED]
    (artifact,) = [e.artifact_update.artifact for e in events if e.HasField("artifact_update")]
    assert artifact.name == "billing_reply"
    assert "$185.40" in artifact.parts[0].text


async def test_card_numbers_never_reach_the_task_store(
    sdk_client: MakeClient, app: Starlette
) -> None:
    client = await sdk_client(streaming=False)
    request = _message(
        "My card is 4111 1111 1111 1111, CVV 123", patient_id="P-1002", verified=True
    )

    (response,) = [event async for event in client.send_message(request)]
    stored = await client.get_task(GetTaskRequest(id=response.task.id))

    (user_message,) = [m for m in stored.history if m.role == Role.ROLE_USER]
    assert get_message_text(user_message) == "My card is **** **** **** ****, CVV ***"
    assert app.state.ledger.sent_links == ()


async def test_tasks_cannot_be_listed_with_the_shared_token(
    http: httpx.AsyncClient, app: Starlette
) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"}
    sent = await http.post("/", json=_rpc("Which insurance do you accept?"), headers=headers)
    assert sent.status_code == 200

    listed = await http.post(
        "/",
        json={"jsonrpc": "2.0", "id": "2", "method": "ListTasks", "params": {}},
        headers=headers,
    )

    assert listed.status_code == 200
    body = listed.json()
    assert "result" not in body and body["error"]["message"]
    assert sent.json()["result"]["task"]["id"] not in listed.text
