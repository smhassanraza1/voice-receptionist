"""Request/response models for the web API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Mode = Literal["realtime", "pipeline"]
Language = Literal["auto", "en", "es", "fr", "de", "hi", "ja"]
EscalationStatus = Literal["open", "resolved"]

# LiveKit room names we accept from clients: what we mint (`call-3f9a…`) and what the SIP
# dispatch rule creates (`call-_+15551234567_AbCd`), without letting arbitrary strings into
# tokens or logs.
ROOM_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_+.\-]{0,127}$"


class TokenRequest(BaseModel):
    """Body of `POST /api/token`; also the agent dispatch metadata, verbatim."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode = "realtime"
    language: Language = "auto"

    def dispatch_metadata(self) -> str:
        """JSON handed to the agent as job metadata (same fields as the request)."""
        return self.model_dump_json()


class ConnectionDetails(BaseModel):
    """What the browser needs to join a room (camelCase, as LiveKit's token endpoints use)."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    server_url: str = Field(alias="serverUrl")
    participant_token: str = Field(alias="participantToken")
    room_name: str = Field(alias="roomName")
    participant_name: str = Field(alias="participantName")


class EscalationCreate(BaseModel):
    """Body the voice agent posts when a browser caller asks for a human."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    room: str = Field(pattern=ROOM_NAME_PATTERN)
    reason: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=4000)


class Escalation(BaseModel):
    id: str
    room: str
    reason: str
    summary: str
    created_at: datetime
    status: EscalationStatus = "open"
    updated_at: datetime | None = None
    resolved_at: datetime | None = None


class OperatorTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    room: str = Field(pattern=ROOM_NAME_PATTERN)
    # Checked like the X-Operator-Passcode header, so it isn't stripped here.
    passcode: Annotated[
        str, StringConstraints(strip_whitespace=False, min_length=1, max_length=256)
    ]
    name: str = Field(min_length=1, max_length=40)
