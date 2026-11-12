import time
from datetime import timedelta
from pathlib import Path

import jwt
import pytest
from pydantic import ValidationError

from receptionist.mcp_server import __main__ as mcp_main
from receptionist.mcp_server import mint_token as mint_token_cli
from receptionist.mcp_server.auth import ALL_SCOPES, KB_READ, JWTTokenVerifier, mint_token
from receptionist.mcp_server.settings import TOKEN_ISSUER, Settings

from .conftest import JWT_SECRET

AUDIENCE = "http://127.0.0.1:8765/mcp"


@pytest.fixture
def verifier() -> JWTTokenVerifier:
    return JWTTokenVerifier(JWT_SECRET, audience=AUDIENCE, leeway_seconds=0)


def signed(claims: dict, secret: str | None = JWT_SECRET, algorithm: str = "HS256") -> str:
    now = int(time.time())
    base = {"iss": TOKEN_ISSUER, "aud": AUDIENCE, "sub": "voice-agent", "iat": now, "exp": now + 60}
    return jwt.encode({**base, **claims}, secret, algorithm=algorithm)


async def test_minted_tokens_verify(verifier: JWTTokenVerifier) -> None:
    token = mint_token(JWT_SECRET, subject="voice-agent", audience=AUDIENCE)
    access = await verifier.verify_token(token)
    assert access is not None
    assert (access.client_id, access.resource) == ("voice-agent", AUDIENCE)
    assert access.scopes == list(ALL_SCOPES)
    assert access.expires_at > time.time()


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(signed({}, secret="another-secret-" + "y" * 32), id="bad-signature"),
        pytest.param(signed({"exp": int(time.time()) - 5}), id="expired"),
        pytest.param(signed({"nbf": int(time.time()) + 300}), id="not-yet-valid"),
        pytest.param(signed({"aud": "http://other.example/mcp"}), id="wrong-audience"),
        pytest.param(signed({"iss": "someone-else"}), id="wrong-issuer"),
        pytest.param(signed({"sub": None}), id="no-subject"),
        pytest.param(signed({}, secret=None, algorithm="none"), id="unsigned-alg-none"),
        pytest.param(
            jwt.encode({"iss": TOKEN_ISSUER, "aud": AUDIENCE, "sub": "x"}, JWT_SECRET),
            id="no-expiry",
        ),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
async def test_invalid_tokens_are_rejected(verifier: JWTTokenVerifier, token: str) -> None:
    assert await verifier.verify_token(token) is None


async def test_unknown_scopes_are_dropped(verifier: JWTTokenVerifier) -> None:
    access = await verifier.verify_token(signed({"scope": "kb:read admin:everything"}))
    assert access is not None and access.scopes == [KB_READ]


def test_mint_token_rejects_unknown_scopes() -> None:
    with pytest.raises(ValueError, match="unknown scopes"):
        mint_token(JWT_SECRET, subject="x", audience=AUDIENCE, scopes=["admin"])


def test_mint_token_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("MCP_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("MCP_SERVER_URL", AUDIENCE)
    mint_token_cli.main(
        ["--sub", "operator", "--scopes", "kb:read,appointments:read", "--ttl-hours", "1"]
    )
    claims = jwt.decode(capsys.readouterr().out.strip(), JWT_SECRET, ["HS256"], audience=AUDIENCE)
    assert claims["sub"] == "operator"
    assert claims["scope"] == "kb:read appointments:read"
    assert claims["exp"] - claims["iat"] == timedelta(hours=1).total_seconds()


def test_short_secrets_are_refused() -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(_env_file=None, jwt_secret="too-short")


def test_server_refuses_to_start_with_a_blank_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `MCP_JWT_SECRET=` copied blank from .env.example counts as unset: the server exits, unserved.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_JWT_SECRET", "")
    monkeypatch.setattr(mcp_main, "configure_logging", lambda level: None)
    monkeypatch.setattr(mcp_main.uvicorn, "run", lambda *a, **k: pytest.fail("served"))
    with pytest.raises(SystemExit, match="MCP_JWT_SECRET is not set"):
        mcp_main.main([])
