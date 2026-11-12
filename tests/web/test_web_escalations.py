"""Escalation intake (agent -> web server) and the operator API around it."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from starlette.testclient import TestClient

from receptionist.web.app import secrets_match

ESCALATION = {
    "room": "call-0a1b2c3d4e5f",
    "reason": "Caller wants to speak with a nurse about a medication reaction",
    "summary": "Maria Lopez, DOB 1984-03-12. Booked Thu 10:30 with Dr. Marquez; asked for a human.",
}


# -- intake auth ---------------------------------------------------------------------------------


def test_intake_requires_internal_token(client: TestClient) -> None:
    missing = client.post("/api/escalations", json=ESCALATION)
    wrong = client.post("/api/escalations", json=ESCALATION, headers={"X-Internal-Token": "nope"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["error"] == "invalid_internal_token"


def test_operator_passcode_does_not_open_intake(
    client: TestClient, operator_headers: dict[str, str], operator_passcode: str
) -> None:
    resp = client.post(
        "/api/escalations", json=ESCALATION, headers={"X-Internal-Token": operator_passcode}
    )
    assert resp.status_code == 401


def test_intake_disabled_without_configured_token(
    web_env: pytest.MonkeyPatch, make_client: Callable[[], TestClient]
) -> None:
    web_env.setenv("INTERNAL_API_TOKEN", "")
    resp = make_client().post("/api/escalations", json=ESCALATION, headers={"X-Internal-Token": ""})
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.parametrize(
    "patch",
    [
        {"room": "call 1; DROP"},
        {"room": ""},
        {"reason": ""},
        {"reason": "x" * 501},
        {"summary": "x" * 4001},
    ],
)
def test_intake_validates_body(
    client: TestClient, internal_headers: dict[str, str], patch: dict[str, str]
) -> None:
    resp = client.post("/api/escalations", json={**ESCALATION, **patch}, headers=internal_headers)
    assert resp.status_code == 422


def test_secrets_match_is_exact() -> None:
    assert secrets_match("abc", "abc")
    assert not secrets_match("abc", "abcd")
    assert not secrets_match("", "abc")
    assert secrets_match("pässcode", "pässcode")


# -- lifecycle ---------------------------------------------------------------------------------


def test_escalation_lifecycle(
    client: TestClient, internal_headers: dict[str, str], operator_headers: dict[str, str]
) -> None:
    created = client.post("/api/escalations", json=ESCALATION, headers=internal_headers)
    assert created.status_code == 201
    item = created.json()
    assert item["id"].startswith("esc_")
    assert item["status"] == "open"
    assert {k: item[k] for k in ("room", "reason", "summary")} == ESCALATION
    assert item["created_at"].endswith("Z") or item["created_at"].endswith("+00:00")

    listed = client.get("/api/escalations", headers=operator_headers)
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert [e["id"] for e in listed.json()["escalations"]] == [item["id"]]

    # The agent escalating the same call again refreshes the open item instead of duplicating.
    again = client.post(
        "/api/escalations",
        json={**ESCALATION, "reason": "Caller is upset and asked again"},
        headers=internal_headers,
    )
    assert again.status_code == 200
    assert again.json()["id"] == item["id"]
    assert again.json()["reason"] == "Caller is upset and asked again"
    assert again.json()["updated_at"] is not None

    resolved = client.post(f"/api/escalations/{item['id']}/resolve", headers=operator_headers)
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_at"] is not None

    # Resolving twice is harmless.
    twice = client.post(f"/api/escalations/{item['id']}/resolve", headers=operator_headers)
    assert twice.status_code == 200
    assert twice.json()["resolved_at"] == resolved.json()["resolved_at"]

    open_only = client.get("/api/escalations?status=open", headers=operator_headers).json()
    assert open_only["escalations"] == []
    resolved_only = client.get("/api/escalations?status=resolved", headers=operator_headers)
    assert [e["id"] for e in resolved_only.json()["escalations"]] == [item["id"]]

    # A new request after resolution opens a new escalation for the same room.
    reopened = client.post("/api/escalations", json=ESCALATION, headers=internal_headers)
    assert reopened.status_code == 201
    assert reopened.json()["id"] != item["id"]


def test_list_is_newest_first(
    client: TestClient, internal_headers: dict[str, str], operator_headers: dict[str, str]
) -> None:
    rooms = ["call-aaaaaaaaaaaa", "call-bbbbbbbbbbbb", "call-cccccccccccc"]
    for room in rooms:
        client.post("/api/escalations", json={**ESCALATION, "room": room}, headers=internal_headers)
    listed = client.get("/api/escalations", headers=operator_headers).json()["escalations"]
    assert [e["room"] for e in listed] == list(reversed(rooms))


def test_resolve_unknown_escalation_is_404(
    client: TestClient, operator_headers: dict[str, str]
) -> None:
    resp = client.post("/api/escalations/esc_000000000000/resolve", headers=operator_headers)
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_invalid_status_filter(client: TestClient, operator_headers: dict[str, str]) -> None:
    assert client.get("/api/escalations?status=all", headers=operator_headers).status_code == 422


# -- operator auth -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/escalations"),
        ("GET", "/api/escalations/stream"),
        ("POST", "/api/escalations/esc_123/resolve"),
    ],
)
def test_operator_endpoints_require_passcode(client: TestClient, method: str, path: str) -> None:
    missing = client.request(method, path)
    wrong = client.request(method, path, headers={"X-Operator-Passcode": "guess"})
    assert missing.status_code == wrong.status_code == 401
    assert wrong.json()["error"] == "invalid_passcode"


def test_internal_token_is_not_an_operator_passcode(
    client: TestClient, internal_headers: dict[str, str]
) -> None:
    resp = client.get(
        "/api/escalations", headers={"X-Operator-Passcode": internal_headers["X-Internal-Token"]}
    )
    assert resp.status_code == 401


def test_operator_console_disabled_without_passcode(
    web_env: pytest.MonkeyPatch, make_client: Callable[[], TestClient]
) -> None:
    web_env.delenv("OPERATOR_PASSCODE")
    resp = make_client().get("/api/escalations", headers={"X-Operator-Passcode": "anything"})
    assert resp.status_code == 503


def test_repeated_wrong_passcodes_are_locked_out(
    web_env: pytest.MonkeyPatch,
    make_client: Callable[[], TestClient],
    operator_headers: dict[str, str],
) -> None:
    web_env.setenv("WEB_AUTH_FAILURE_LIMIT", "3")
    client = make_client()
    for _ in range(3):
        bad = client.get("/api/escalations", headers={"X-Operator-Passcode": "guess"})
        assert bad.status_code == 401
    # Even the right passcode waits out the lockout window.
    locked = client.get("/api/escalations", headers=operator_headers)
    assert locked.status_code == 429
    assert "retry-after" in locked.headers


def test_passcode_lockout_does_not_block_escalation_intake(
    web_env: pytest.MonkeyPatch,
    make_client: Callable[[], TestClient],
    internal_headers: dict[str, str],
) -> None:
    """Operator typos and the agent share an address on a single host; they're counted apart."""
    web_env.setenv("WEB_AUTH_FAILURE_LIMIT", "3")
    client = make_client()
    for _ in range(3):
        client.get("/api/escalations", headers={"X-Operator-Passcode": "guess"})
    assert client.get("/api/escalations", headers={"X-Operator-Passcode": "x"}).status_code == 429
    resp = client.post("/api/escalations", json=ESCALATION, headers=internal_headers)
    assert resp.status_code == 201


def test_internal_token_lockout_does_not_block_operators(
    web_env: pytest.MonkeyPatch,
    make_client: Callable[[], TestClient],
    operator_headers: dict[str, str],
) -> None:
    web_env.setenv("WEB_AUTH_FAILURE_LIMIT", "2")
    client = make_client()
    for _ in range(2):
        client.post("/api/escalations", json=ESCALATION, headers={"X-Internal-Token": "guess"})
    assert (
        client.post("/api/escalations", json=ESCALATION, headers={"X-Internal-Token": "x"})
    ).status_code == 429
    assert client.get("/api/escalations", headers=operator_headers).status_code == 200


def test_configured_secrets_ignore_surrounding_whitespace(
    web_env: pytest.MonkeyPatch, make_client: Callable[[], TestClient]
) -> None:
    web_env.setenv("OPERATOR_PASSCODE", "  harbor-street-200 ")
    web_env.setenv("INTERNAL_API_TOKEN", "internal-token\n")
    client = make_client()
    assert (
        client.get(
            "/api/escalations", headers={"X-Operator-Passcode": "harbor-street-200"}
        ).status_code
        == 200
    )
    resp = client.post(
        "/api/escalations", json=ESCALATION, headers={"X-Internal-Token": "internal-token"}
    )
    assert resp.status_code == 201


def test_intake_requires_a_json_content_type(
    client: TestClient, internal_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/api/escalations",
        content=b'{"room": "call-0a1b2c3d4e5f", "reason": "x"}',
        headers={**internal_headers, "Content-Type": "text/plain"},
    )
    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_media_type"


@pytest.mark.parametrize("room", ["call-0a1b2c3d4e5f", "call-_+15551234567_AbCdEf"])
def test_intake_accepts_web_and_sip_room_names(
    client: TestClient, internal_headers: dict[str, str], room: str
) -> None:
    resp = client.post(
        "/api/escalations", json={**ESCALATION, "room": room}, headers=internal_headers
    )
    assert resp.status_code == 201
