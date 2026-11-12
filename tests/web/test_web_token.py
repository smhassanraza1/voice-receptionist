"""`POST /api/token`: browser caller tokens with agent dispatch."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from livekit import api
from starlette.testclient import TestClient

TokenVerifier = Callable[[str], api.access_token.Claims]


def test_token_embeds_dispatch_and_scoped_grants(
    client: TestClient, verify_token: TokenVerifier, livekit_url: str
) -> None:
    body = {"mode": "pipeline", "language": "es"}
    resp = client.post("/api/token", json=body)

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()
    assert set(data) == {"serverUrl", "participantToken", "roomName", "participantName"}
    assert data["serverUrl"] == livekit_url
    assert re.fullmatch(r"call-[0-9a-f]{12}", data["roomName"])

    claims = verify_token(data["participantToken"])
    assert re.fullmatch(r"caller-[0-9a-f]{8}", claims.identity)
    assert claims.name == data["participantName"]

    video = claims.video
    assert video is not None
    assert video.room == data["roomName"]
    assert video.room_join is True
    assert video.can_publish is True and video.can_subscribe is True
    assert video.can_publish_data is True
    assert video.can_publish_sources == ["microphone"]
    # Nothing beyond joining this one room.
    assert not video.room_admin and not video.room_create and not video.room_list
    assert not video.room_record and not video.ingress_admin and not video.agent

    assert claims.room_config is not None
    (dispatch,) = claims.room_config.agents
    assert dispatch.agent_name == "receptionist"
    assert json.loads(dispatch.metadata) == body


def test_token_ttl_is_fifteen_minutes(
    client: TestClient, raw_claims: Callable[[str], dict[str, Any]]
) -> None:
    token = client.post("/api/token", json={"mode": "realtime"}).json()["participantToken"]
    claims = raw_claims(token)
    assert claims["exp"] - claims["nbf"] == 15 * 60
    # camelCase room config as LiveKit server expects it in the JWT
    assert claims["roomConfig"]["agents"][0]["agentName"] == "receptionist"


def test_empty_body_uses_contract_defaults(client: TestClient, verify_token: TokenVerifier) -> None:
    resp = client.post("/api/token", content=b"", headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    claims = verify_token(resp.json()["participantToken"])
    assert claims.room_config is not None
    assert json.loads(claims.room_config.agents[0].metadata) == {
        "mode": "realtime",
        "language": "auto",
    }


def test_every_call_gets_a_fresh_room_and_identity(
    client: TestClient, verify_token: TokenVerifier
) -> None:
    first, second = (client.post("/api/token", json={}).json() for _ in range(2))
    assert first["roomName"] != second["roomName"]
    assert (
        verify_token(first["participantToken"]).identity
        != verify_token(second["participantToken"]).identity
    )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"mode": "duplex"}, "mode"),
        ({"language": "it"}, "language"),
        ({"mode": "realtime", "room": "call-mine"}, "room"),  # clients can't pick their room
    ],
)
def test_invalid_body_is_rejected(client: TestClient, body: dict[str, str], field: str) -> None:
    resp = client.post("/api/token", json=body)
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "validation_error"
    assert [d["loc"] for d in data["detail"]] == [[field]]


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_token_requires_a_json_content_type(client: TestClient, content_type: str | None) -> None:
    """Cross-site "simple" requests can't mint tokens: the JSON media type forces a preflight."""
    headers = {"Content-Type": content_type} if content_type else {}
    resp = client.post("/api/token", content=b'{"mode": "realtime"}', headers=headers)
    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_media_type"


def test_json_content_type_parameters_are_accepted(client: TestClient) -> None:
    resp = client.post(
        "/api/token",
        content=b"{}",
        headers={"Content-Type": "Application/JSON; charset=utf-8"},
    )
    assert resp.status_code == 200


def test_dispatches_the_agent_named_by_agent_name(
    web_env: pytest.MonkeyPatch, make_client: Callable[[], TestClient], verify_token: TokenVerifier
) -> None:
    web_env.setenv("AGENT_NAME", "receptionist-staging")
    token = make_client().post("/api/token", json={}).json()["participantToken"]
    claims = verify_token(token)
    assert claims.room_config is not None
    assert claims.room_config.agents[0].agent_name == "receptionist-staging"


def test_agent_name_and_options_match_the_agent() -> None:
    """The web server and the worker read AGENT_NAME with the same default, and accept the same
    modes and languages."""
    from typing import get_args

    from receptionist.agent.settings import SUPPORTED_LANGUAGES, AgentSettings, Mode
    from receptionist.web import WebSettings
    from receptionist.web.models import Language
    from receptionist.web.models import Mode as WebMode

    assert (
        WebSettings.model_fields["agent_name"].default
        == AgentSettings.model_fields["agent_name"].default
    )
    assert get_args(Language) == SUPPORTED_LANGUAGES
    assert get_args(WebMode) == get_args(Mode)


def test_malformed_json_is_a_400(client: TestClient) -> None:
    resp = client.post(
        "/api/token", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_json"


def test_non_object_json_is_a_422(client: TestClient) -> None:
    assert client.post("/api/token", json=["realtime"]).status_code == 422


def test_wrong_method_is_a_json_405(client: TestClient) -> None:
    resp = client.get("/api/token")
    assert resp.status_code == 405
    assert resp.json()["error"] == "method_not_allowed"


def test_missing_livekit_credentials_returns_503(
    web_env: pytest.MonkeyPatch, make_client: Callable[[], TestClient]
) -> None:
    web_env.delenv("LIVEKIT_API_SECRET")
    resp = make_client().post("/api/token", json={})
    assert resp.status_code == 503
    assert resp.json()["error"] == "livekit_not_configured"


def test_token_endpoint_is_rate_limited_per_client(
    web_env: pytest.MonkeyPatch, make_client: Callable[[], TestClient]
) -> None:
    web_env.setenv("WEB_TOKEN_RATE_LIMIT", "2")
    client = make_client()
    assert [client.post("/api/token", json={}).status_code for _ in range(2)] == [200, 200]
    limited = client.post("/api/token", json={})
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1


def test_oversized_body_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/token",
        content=b"{" + b" " * (70 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
