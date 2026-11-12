"""Starlette app: LiveKit tokens, escalation queue + operator API, and the built frontend."""

from __future__ import annotations

import contextlib
import hmac
import logging
import math
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import BaseRoute, Match, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .escalations import EscalationStore
from .models import EscalationCreate, OperatorTokenRequest, TokenRequest
from .ratelimit import SlidingWindowLimiter
from .settings import WebSettings
from .sse import escalation_events
from .tokens import LiveKitNotConfiguredError, build_caller_token, build_operator_token

logger = logging.getLogger("receptionist.web")

INTERNAL_TOKEN_HEADER = "X-Internal-Token"
OPERATOR_PASSCODE_HEADER = "X-Operator-Passcode"
MAX_BODY_BYTES = 64 * 1024

# Hosts LiveKit Cloud serves a project from. The SDK fetches `/settings/regions` from the project
# URL and may then connect to a regional host (`<project>.<region>.production.livekit.cloud`).
LIVEKIT_CLOUD_DOMAINS = ("livekit.cloud", "livekit.run")
GOOGLE_FONTS_CSS = "https://fonts.googleapis.com"
GOOGLE_FONTS_FILES = "https://fonts.gstatic.com"


def livekit_connect_sources(livekit_url: str) -> list[str]:
    """CSP sources for the LiveKit server: its WebSocket origin plus the HTTP(S) twin the SDK uses
    for region lookup and connectivity checks."""
    parts = urlsplit(livekit_url)
    if not parts.hostname or parts.scheme not in ("ws", "wss", "http", "https"):
        return []
    secure = parts.scheme in ("wss", "https")
    ws, http = ("wss", "https") if secure else ("ws", "http")
    hostname = parts.hostname
    cloud = next((d for d in LIVEKIT_CLOUD_DOMAINS if hostname.endswith("." + d)), None)
    hosts = [f"*.{cloud}"] if cloud else [parts.netloc.rpartition("@")[2]]
    return [f"{scheme}://{h}" for h in hosts for scheme in (ws, http)]


def content_security_policy(livekit_url: str) -> str:
    """Policy for the built SPA: own scripts and styles, Google Fonts, and the LiveKit server.

    WebRTC media isn't governed by CSP, and call audio plays from `srcObject` MediaStreams, so
    nothing beyond `connect-src` is needed for LiveKit.
    """
    directives = {
        "default-src": ["'self'"],
        "script-src": ["'self'"],
        "style-src": ["'self'", GOOGLE_FONTS_CSS],
        "font-src": ["'self'", GOOGLE_FONTS_FILES],
        # data: for the inline SVG grain texture in base.css
        "img-src": ["'self'", "data:"],
        "connect-src": ["'self'", *livekit_connect_sources(livekit_url)],
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
    }
    return "; ".join(f"{name} {' '.join(sources)}" for name, sources in directives.items())


def security_headers(settings: WebSettings) -> dict[str, str]:
    return {
        "Content-Security-Policy": content_security_policy(settings.livekit_url),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    }


class ApiError(Exception):
    """An error rendered as `{"error": code, "message": ...}` with the given status."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        detail: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        self.headers = dict(headers or {})


@dataclass(slots=True)
class AppContext:
    settings: WebSettings
    store: EscalationStore
    token_limiter: SlidingWindowLimiter
    # Failed attempts are counted per secret, so an operator mistyping the passcode can't lock the
    # voice agent out of filing escalations from the same address (or the other way round).
    operator_auth_failures: SlidingWindowLimiter
    internal_auth_failures: SlidingWindowLimiter


# -- helpers -----------------------------------------------------------------------------------


def _ctx(request: Request) -> AppContext:
    return cast(AppContext, request.app.state.ctx)


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def secrets_match(supplied: str, expected: str) -> bool:
    """Constant-time comparison so response timing doesn't leak how much of a secret matched."""
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _too_many(retry_after: float, message: str) -> ApiError:
    return ApiError(
        429,
        "rate_limited",
        message,
        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
    )


def _check_secret(
    request: Request,
    *,
    supplied: str | None,
    expected: str | None,
    failures: SlidingWindowLimiter,
    disabled_message: str,
    invalid_code: str,
    invalid_message: str,
) -> None:
    """Compare a secret, locking a client out after repeated failures.

    A locked-out client is refused before the comparison, even with the right secret: otherwise the
    lockout would still answer "right" or "wrong" to every guess. Surrounding whitespace is ignored
    wherever the secret arrives (HTTP already drops it from header values).
    """
    if expected is None:
        raise ApiError(503, "not_configured", disabled_message)
    key = client_key(request)
    retry = failures.retry_after(key)
    if retry is not None:
        raise _too_many(retry, "Too many failed attempts; try again later")
    supplied = (supplied or "").strip()
    if not supplied or not secrets_match(supplied, expected):
        failures.record(key)
        raise ApiError(401, invalid_code, invalid_message)


def require_operator(request: Request, supplied: str | None = None) -> None:
    if supplied is None:
        supplied = request.headers.get(OPERATOR_PASSCODE_HEADER)
    _check_secret(
        request,
        supplied=supplied,
        expected=_ctx(request).settings.operator_secret,
        failures=_ctx(request).operator_auth_failures,
        disabled_message="The operator console is disabled: set OPERATOR_PASSCODE",
        invalid_code="invalid_passcode",
        invalid_message="Operator passcode is missing or incorrect",
    )


def require_internal(request: Request) -> None:
    _check_secret(
        request,
        supplied=request.headers.get(INTERNAL_TOKEN_HEADER),
        expected=_ctx(request).settings.internal_token,
        failures=_ctx(request).internal_auth_failures,
        disabled_message="Escalation intake is disabled: set INTERNAL_API_TOKEN",
        invalid_code="invalid_internal_token",
        invalid_message=f"{INTERNAL_TOKEN_HEADER} header is missing or incorrect",
    )


def _is_json(request: Request) -> bool:
    media_type = request.headers.get("content-type", "").partition(";")[0]
    return media_type.strip().lower() == "application/json"


async def read_model[M: BaseModel](
    request: Request, model: type[M], *, empty_as: bytes | None = None
) -> M:
    # Requiring the JSON media type means a cross-site page can't reach these endpoints with a
    # "simple" form or text/plain POST: the browser has to preflight, and there's no CORS here.
    if not _is_json(request):
        raise ApiError(415, "unsupported_media_type", "Send the body as application/json")
    raw = await request.body()
    if not raw.strip():
        if empty_as is None:
            raise ApiError(400, "invalid_json", "Request body must be a JSON object")
        raw = empty_as
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False, include_context=False)
        if any(err["type"] == "json_invalid" for err in errors):
            raise ApiError(400, "invalid_json", "Request body is not valid JSON") from None
        detail = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in errors]
        raise ApiError(
            422, "validation_error", "Request body failed validation", detail=detail
        ) from None


def _no_store(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


# -- endpoints ---------------------------------------------------------------------------------


async def healthz(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    return JSONResponse(
        {
            "status": "ok",
            "livekit": ctx.settings.livekit_configured,
            "escalation_intake": ctx.settings.internal_token is not None,
            "operator_console": ctx.settings.operator_secret is not None,
            "frontend": (ctx.settings.web_static_dir / "index.html").is_file(),
        }
    )


async def create_caller_token(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    retry = ctx.token_limiter.hit(client_key(request))
    if retry is not None:
        raise _too_many(retry, "Too many call attempts; wait a moment and try again")
    body = await read_model(request, TokenRequest, empty_as=b"{}")
    try:
        details = build_caller_token(ctx.settings, body)
    except LiveKitNotConfiguredError as exc:
        raise ApiError(503, "livekit_not_configured", str(exc)) from None
    logger.info(
        "caller token issued room=%s mode=%s language=%s",
        details.room_name,
        body.mode,
        body.language,
    )
    return _no_store(details.model_dump())


async def submit_escalation(request: Request) -> JSONResponse:
    require_internal(request)
    body = await read_model(request, EscalationCreate)
    item, created = _ctx(request).store.submit(body)
    return _no_store(item.model_dump(mode="json"), status_code=201 if created else 200)


async def list_escalations(request: Request) -> JSONResponse:
    require_operator(request)
    status = request.query_params.get("status")
    if status not in (None, "open", "resolved"):
        raise ApiError(422, "validation_error", "status must be 'open' or 'resolved'")
    items = _ctx(request).store.list(status)  # type: ignore[arg-type]
    return _no_store({"escalations": [e.model_dump(mode="json") for e in items]})


async def stream_escalations(request: Request) -> StreamingResponse:
    # EventSource can't send headers; the console reads this stream with fetch() instead, so
    # the passcode stays out of URLs and access logs.
    require_operator(request)
    return StreamingResponse(
        escalation_events(_ctx(request).store),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def resolve_escalation(request: Request) -> JSONResponse:
    require_operator(request)
    escalation_id = request.path_params["escalation_id"]
    item = _ctx(request).store.resolve(escalation_id)
    if item is None:
        raise ApiError(404, "not_found", f"No escalation with id {escalation_id!r}")
    return _no_store(item.model_dump(mode="json"))


async def create_operator_token(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    body = await read_model(request, OperatorTokenRequest)
    require_operator(request, body.passcode)
    if ctx.store.find_open(body.room) is None:
        raise ApiError(404, "unknown_room", "No open escalation for that room")
    try:
        details = build_operator_token(ctx.settings, room=body.room, name=body.name)
    except LiveKitNotConfiguredError as exc:
        raise ApiError(503, "livekit_not_configured", str(exc)) from None
    logger.info("operator token issued room=%s", body.room)
    return _no_store(details.model_dump())


# -- errors & middleware -----------------------------------------------------------------------


async def handle_api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    payload: dict[str, Any] = {"error": exc.code, "message": exc.message}
    if exc.detail is not None:
        payload["detail"] = exc.detail
    return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    if exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=exc.headers)
    code = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}.get(
        exc.status_code, "http_error"
    )
    return JSONResponse(
        {"error": code, "message": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


class SecurityHeadersMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so streaming responses pass through untouched."""

    def __init__(self, app: ASGIApp, *, headers: Mapping[str, str]) -> None:
        self.app = app
        self.headers = dict(headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


# -- frontend ----------------------------------------------------------------------------------


class FrontendFiles(StaticFiles):
    """Vite output: content-hashed `/assets/*` are immutable, everything else revalidates."""

    def file_response(
        self,
        full_path: os.PathLike[str] | str,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        if scope["path"].startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


class FrontendMount(Mount):
    """Serve the SPA at `/` without shadowing API paths (so they keep proper 404/405s)."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] == "http" and scope["path"].startswith(("/api/", "/healthz")):
            return Match.NONE, {}
        return super().matches(scope)


_NOT_BUILT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Frontend not built</title></head>
<body>
<h1>Frontend not built</h1>
<p>The API is running, but <code>web/dist</code> doesn't exist yet.</p>
<p>Build it with <code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code>, or run the Vite dev
server (<code>npm run dev</code>) and open <a href="http://localhost:5173">localhost:5173</a>,
which proxies <code>/api</code> here.</p>
</body></html>"""


def frontend_routes(static_dir: Path) -> list[BaseRoute]:
    index = static_dir / "index.html"
    if not index.is_file():

        async def not_built(request: Request) -> HTMLResponse:
            return HTMLResponse(_NOT_BUILT_PAGE, status_code=503)

        return [Route("/", not_built), Route("/operator", not_built)]

    async def spa_entry(request: Request) -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return [
        Route("/operator", spa_entry),
        Route("/operator/", spa_entry),
        FrontendMount("/", app=FrontendFiles(directory=static_dir, html=True), name="frontend"),
    ]


# -- factory -----------------------------------------------------------------------------------


def create_app(
    settings: WebSettings | None = None, *, store: EscalationStore | None = None
) -> Starlette:
    settings = settings or WebSettings()
    ctx = AppContext(
        settings=settings,
        store=store or EscalationStore(),
        token_limiter=SlidingWindowLimiter(
            settings.web_token_rate_limit, settings.web_token_rate_window_seconds
        ),
        operator_auth_failures=SlidingWindowLimiter(
            settings.web_auth_failure_limit, settings.web_auth_failure_window_seconds
        ),
        internal_auth_failures=SlidingWindowLimiter(
            settings.web_auth_failure_limit, settings.web_auth_failure_window_seconds
        ),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if not settings.livekit_configured:
            logger.warning("LIVEKIT_URL/API_KEY/API_SECRET not set: /api/token will return 503")
        if settings.internal_token is None:
            logger.warning("INTERNAL_API_TOKEN not set: escalation intake is disabled")
        if settings.operator_secret is None:
            logger.warning("OPERATOR_PASSCODE not set: the operator console is disabled")
        try:
            yield
        finally:
            ctx.store.close()

    routes: list[BaseRoute] = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/token", create_caller_token, methods=["POST"]),
        Route("/api/escalations", submit_escalation, methods=["POST"]),
        Route("/api/escalations", list_escalations, methods=["GET"]),
        Route("/api/escalations/stream", stream_escalations, methods=["GET"]),
        Route("/api/escalations/{escalation_id:str}/resolve", resolve_escalation, methods=["POST"]),
        Route("/api/operator/token", create_operator_token, methods=["POST"]),
        *frontend_routes(settings.web_static_dir),
    ]

    app = Starlette(
        routes=routes,
        exception_handlers={ApiError: handle_api_error, HTTPException: handle_http_exception},
        lifespan=lifespan,
        max_body_size=MAX_BODY_BYTES,
    )
    app.add_middleware(SecurityHeadersMiddleware, headers=security_headers(settings))
    app.state.ctx = ctx
    return app
