"""LiveKit access tokens for browser callers and human operators."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import timedelta

from livekit import api

from .models import ConnectionDetails, TokenRequest
from .settings import WebSettings

CALLER_DISPLAY_NAME = "Web caller"
OPERATOR_IDENTITY_PREFIX = "operator-"

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")


class LiveKitNotConfiguredError(RuntimeError):
    """Raised when a token is requested but LIVEKIT_URL / API key / secret are missing."""


def new_room_name() -> str:
    return f"call-{uuid.uuid4().hex[:12]}"


def new_caller_identity() -> str:
    return f"caller-{secrets.token_hex(4)}"


def operator_identity(name: str) -> str:
    """`operator-<slug>-<random>`; the agent goes quiet when an identity with this prefix joins.

    The random part keeps two operators with the same name (or one operator in two tabs) from
    sharing an identity, which would make LiveKit disconnect the first to join.
    """
    slug = _SLUG_INVALID.sub("-", name.lower()).strip("-")[:32].rstrip("-")
    suffix = secrets.token_hex(3)
    return (
        f"{OPERATOR_IDENTITY_PREFIX}{slug}-{suffix}" if slug else OPERATOR_IDENTITY_PREFIX + suffix
    )


def _token(settings: WebSettings, *, identity: str, name: str, room: str) -> api.AccessToken:
    if not settings.livekit_configured:
        raise LiveKitNotConfiguredError(
            "LiveKit is not configured: set LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET"
        )
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        # Voice only. Data stays on: the pre-connect audio buffer is sent as a byte stream.
        can_publish_sources=["microphone"],
        can_publish_data=True,
        can_subscribe=True,
    )
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret.get_secret_value())
        .with_identity(identity)
        .with_name(name)
        .with_ttl(timedelta(seconds=settings.web_token_ttl_seconds))
        .with_grants(grants)
    )


def build_caller_token(settings: WebSettings, request: TokenRequest) -> ConnectionDetails:
    """Token for a new browser call; the room's first join dispatches the receptionist agent."""
    room = new_room_name()
    token = _token(
        settings,
        identity=new_caller_identity(),
        name=CALLER_DISPLAY_NAME,
        room=room,
    ).with_room_config(
        api.RoomConfiguration(
            agents=[
                api.RoomAgentDispatch(
                    agent_name=settings.agent_name,
                    metadata=request.dispatch_metadata(),
                )
            ],
        )
    )
    return ConnectionDetails(
        server_url=settings.livekit_url,
        participant_token=token.to_jwt(),
        room_name=room,
        participant_name=CALLER_DISPLAY_NAME,
    )


def build_operator_token(settings: WebSettings, *, room: str, name: str) -> ConnectionDetails:
    """Token for a staff member joining an escalated call. No agent dispatch."""
    token = _token(
        settings, identity=operator_identity(name), name=name, room=room
    ).with_attributes({"role": "operator"})
    return ConnectionDetails(
        server_url=settings.livekit_url,
        participant_token=token.to_jwt(),
        room_name=room,
        participant_name=name,
    )
