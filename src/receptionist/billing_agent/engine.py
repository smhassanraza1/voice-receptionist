"""Types shared by the reasoning engines and the A2A executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from receptionist.billing_agent.sessions import Session


class Caller(BaseModel):
    """Who the receptionist says is calling: the A2A message's data part."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    patient_id: str | None = None
    verified: bool = False
    language: str = "en"

    @property
    def can_access_account(self) -> bool:
        """Account-specific answers need both a verified identity and a patient id."""
        return self.verified and bool(self.patient_id)

    @property
    def account_key(self) -> str | None:
        """The account this caller may see; conversation state is scoped to it."""
        return self.patient_id if self.can_access_account else None


@dataclass(frozen=True, slots=True)
class BillingRequest:
    question: str  # already card-number redacted
    caller: Caller
    card_number_detected: bool = False


@dataclass(frozen=True, slots=True)
class AgentReply:
    text: str
    intent: str
    engine: str
    language: str
    needs_input: bool = False  # the reply asks the caller something (A2A input-required)
    needs_verification: bool = False  # account details were withheld from an unverified caller
    facts: dict[str, Any] = field(default_factory=dict)


class ReasoningEngine(Protocol):
    async def respond(self, request: BillingRequest, session: Session) -> AgentReply: ...
