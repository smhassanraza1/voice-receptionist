"""Bearer-token authentication for the A2A endpoint, and card-number redaction."""

from __future__ import annotations

import hmac
import re
from collections.abc import Collection

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
    SimpleUser,
)
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

# 13-19 digits, optionally grouped with spaces or dashes, as a caller would read a card.
_CARD_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_DIGIT = re.compile(r"\d")


def redact_card_numbers(text: str) -> tuple[str, bool]:
    """Mask every digit in `text` if it contains something shaped like a card number.

    Masking all digits (not just the card) also hides an expiry date or CVV read in the same
    breath. Returns the redacted text and whether anything was found.
    """
    if not _CARD_NUMBER.search(text):
        return text, False
    return _DIGIT.sub("*", text), True


class BearerTokenBackend(AuthenticationBackend):
    """Accepts one shared token, compared in constant time. Public paths skip the check."""

    def __init__(self, token: str, *, public_paths: Collection[str], principal: str) -> None:
        self._token = token.encode()
        self._public_paths = frozenset(public_paths)
        self._principal = principal

    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, BaseUser] | None:
        if conn.url.path in self._public_paths:
            return None
        scheme, _, credentials = conn.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            credentials.strip().encode(), self._token
        ):
            raise AuthenticationError("Missing or invalid bearer token.")
        return AuthCredentials(["authenticated"]), SimpleUser(self._principal)


def unauthorized(conn: HTTPConnection, exc: AuthenticationError) -> Response:
    return JSONResponse(
        {"error": "unauthorized", "message": str(exc)},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="billing-agent"'},
    )
