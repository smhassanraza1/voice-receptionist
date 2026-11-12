from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CallState:
    """Per-call state shared by every agent in the session (`AgentSession.userdata`).

    Identity is only ever set from server-verified MCP results (see `tools.py`), never from
    arguments the LLM produced, so the billing handoff can't be talked into another account.
    """

    room_name: str = ""
    language: str = "en"
    verified_patient_id: str | None = None
    verified_patient_name: str | None = None
    billing_context_id: str | None = None
    escalated: bool = False

    def mark_verified(self, patient_id: str, patient_name: str | None) -> None:
        self.verified_patient_id = patient_id
        self.verified_patient_name = patient_name
