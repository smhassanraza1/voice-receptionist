"""Bearer-token auth: HS256 JWTs minted out of band and verified on every HTTP request."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from datetime import timedelta

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .settings import TOKEN_ISSUER

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"

KB_READ = "kb:read"
APPOINTMENTS_READ = "appointments:read"
APPOINTMENTS_WRITE = "appointments:write"
ALL_SCOPES = (KB_READ, APPOINTMENTS_READ, APPOINTMENTS_WRITE)


class JWTTokenVerifier(TokenVerifier):
    """Accepts HS256 tokens with the expected issuer and audience that are inside exp/nbf.

    The SDK calls `verify_token` for each request; returning `None` makes the transport answer
    401 before any MCP message is parsed. Only known scopes are passed through.
    """

    def __init__(
        self, secret: str, *, audience: str, issuer: str = TOKEN_ISSUER, leeway_seconds: int = 30
    ) -> None:
        self._secret = secret
        self._audience = audience
        self._issuer = issuer
        self._leeway = leeway_seconds

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            # The reason (expired, bad signature, wrong audience...) is for operators only.
            logger.info("Rejected bearer token: %s", type(exc).__name__)
            return None
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject:
            return None
        scopes = [s for s in str(claims.get("scope", "")).split() if s in ALL_SCOPES]
        return AccessToken(
            token=token,
            client_id=subject,
            subject=subject,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self._audience,
            claims={"iss": claims["iss"], "jti": claims.get("jti")},
        )


def mint_token(
    secret: str,
    *,
    subject: str,
    audience: str,
    scopes: Iterable[str] = ALL_SCOPES,
    ttl: timedelta = timedelta(hours=12),
    issuer: str = TOKEN_ISSUER,
) -> str:
    """Sign a token the server will accept. Unknown scopes are rejected early."""
    scopes = list(dict.fromkeys(scopes))
    unknown = set(scopes) - set(ALL_SCOPES)
    if unknown:
        raise ValueError(f"unknown scopes: {', '.join(sorted(unknown))}")
    issued_at = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "scope": " ".join(scopes),
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + int(ttl.total_seconds()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)
