"""`POST /api/operator/token`: a staff member joins an escalated call."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from livekit import api
from starlette.testclient import TestClient

from receptionist.web.tokens import operator_identity

ROOM = "call-5e6f7a8b9c0d"


@pytest.fixture
def escalated_room(client: TestClient, internal_headers: dict[str, str]) -> str:
    resp = client.post(
        "/api/escalations",
        json={"room": ROOM, "reason": "Billing dispute", "summary": "Wants a person."},
        headers=internal_headers,
    )
    assert resp.status_code == 201
    return ROOM


def test_operator_token_joins_the_room_without_dispatch(
    client: TestClient,
    escalated_room: str,
    operator_passcode: str,
    verify_token: Callable[[str], api.access_token.Claims],
    livekit_url: str,
) -> None:
    resp = client.post(
        "/api/operator/token",
        json={"room": escalated_room, "passcode": operator_passcode, "name": "Dana Whitfield"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["roomName"] == escalated_room
    assert data["participantName"] == "Dana Whitfield"
    assert data["serverUrl"] == livekit_url

    claims = verify_token(data["participantToken"])
    assert re.fullmatch(r"operator-dana-whitfield-[0-9a-f]{6}", claims.identity)
    assert claims.name == "Dana Whitfield"
    assert claims.attributes == {"role": "operator"}
    assert claims.video is not None
    assert claims.video.room == escalated_room
    assert claims.video.room_join is True
    assert claims.video.can_publish_sources == ["microphone"]
    assert not claims.video.room_admin
    # The agent is already in the room; joining must not dispatch another one.
    assert claims.room_config is None


def test_operator_token_requires_the_passcode(client: TestClient, escalated_room: str) -> None:
    resp = client.post(
        "/api/operator/token",
        json={"room": escalated_room, "passcode": "letmein", "name": "Dana"},
    )
    assert resp.status_code == 401


def test_operator_token_only_for_escalated_rooms(
    client: TestClient, operator_passcode: str
) -> None:
    resp = client.post(
        "/api/operator/token",
        json={"room": "call-ffffffffffff", "passcode": operator_passcode, "name": "Dana"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown_room"


def test_operator_token_not_issued_after_resolve(
    client: TestClient,
    escalated_room: str,
    operator_passcode: str,
    operator_headers: dict[str, str],
) -> None:
    (item,) = client.get("/api/escalations", headers=operator_headers).json()["escalations"]
    client.post(f"/api/escalations/{item['id']}/resolve", headers=operator_headers)
    resp = client.post(
        "/api/operator/token",
        json={"room": escalated_room, "passcode": operator_passcode, "name": "Dana"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown_room"


def test_operators_with_the_same_name_get_distinct_identities(
    client: TestClient,
    escalated_room: str,
    operator_passcode: str,
    verify_token: Callable[[str], api.access_token.Claims],
) -> None:
    """LiveKit disconnects the earlier participant when a second one joins with its identity."""
    body = {"room": escalated_room, "passcode": operator_passcode, "name": "Dana"}
    first, second = (
        verify_token(client.post("/api/operator/token", json=body).json()["participantToken"])
        for _ in range(2)
    )
    assert first.identity != second.identity
    assert first.identity.startswith("operator-dana-")


def test_body_passcode_ignores_surrounding_whitespace_like_the_header(
    client: TestClient, escalated_room: str, operator_passcode: str
) -> None:
    resp = client.post(
        "/api/operator/token",
        json={"room": escalated_room, "passcode": f" {operator_passcode}\t", "name": "Dana"},
    )
    assert resp.status_code == 200


def test_operator_token_requires_a_json_content_type(
    client: TestClient, escalated_room: str, operator_passcode: str
) -> None:
    resp = client.post(
        "/api/operator/token",
        content=f'{{"room": "{escalated_room}", "passcode": "{operator_passcode}", "name": "D"}}',
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == 415


@pytest.mark.parametrize(
    "body",
    [
        {"room": ROOM, "passcode": "x"},  # no name
        {"room": ROOM, "passcode": "x", "name": "   "},
        {"room": ROOM, "passcode": "x", "name": "n" * 41},
        {"room": "../etc", "passcode": "x", "name": "Dana"},
    ],
)
def test_operator_token_validation(client: TestClient, body: dict[str, str]) -> None:
    resp = client.post("/api/operator/token", json=body)
    assert resp.status_code == 422
    # Validation errors never echo the submitted passcode back.
    assert "input" not in resp.text


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Dana Whitfield", "dana-whitfield"),
        ("  Nurse  Kim!! ", "nurse-kim"),
        ("O'Brien-Smith", "o-brien-smith"),
        ("a" * 60, "a" * 32),
    ],
)
def test_operator_identity_slug(name: str, slug: str) -> None:
    assert re.fullmatch(rf"operator-{slug}-[0-9a-f]{{6}}", operator_identity(name))


def test_operator_identity_falls_back_for_non_latin_names() -> None:
    assert re.fullmatch(r"operator-[0-9a-f]{6}", operator_identity("山田"))
