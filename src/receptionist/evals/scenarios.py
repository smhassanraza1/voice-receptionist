"""Scenario definitions: who calls, what they want, and how the run is graded."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

WRITE_TOOLS = frozenset(
    {
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "transfer_to_billing",
        "transfer_to_human",
    }
)


class Persona(BaseModel):
    name: str
    traits: str = Field(description="Personality and speaking style the simulator should adopt.")
    language: str = "en"
    facts: dict[str, str] = Field(
        default_factory=dict,
        description="Things the caller knows and reveals only when asked (DOB, phone, codes).",
    )
    asr_noise: float = Field(
        default=0.0,
        ge=0,
        le=0.5,
        description="Probability of corrupting each word, to mimic speech-recognition errors.",
    )


class ExpectedCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Outcome(BaseModel):
    """Final-state check, graded through the MCP server's own lookup tool."""

    kind: Literal["booked", "cancelled", "rescheduled", "unchanged", "no_booking"]
    date_of_birth: str
    phone: str | None = None
    confirmation_code: str | None = None
    specialty: str | None = None


class Expected(BaseModel):
    tool_calls: list[ExpectedCall] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)
    handoff: Literal["human", "billing"] | None = None


class Scenario(BaseModel):
    id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    persona: Persona
    goal: str
    expectations: str = Field(description="Rubric for the LLM judge.")
    expected: Expected = Field(default_factory=Expected)
    max_turns: int = 14


def load_scenarios(
    path: Path, *, ids: list[str] | None = None, tags: list[str] | None = None
) -> list[Scenario]:
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    scenarios: list[Scenario] = []
    for file in files:
        raw = yaml.safe_load(file.read_text()) or []
        scenarios.extend(Scenario.model_validate(item) for item in raw)

    seen: set[str] = set()
    for s in scenarios:
        if s.id in seen:
            raise ValueError(f"duplicate scenario id: {s.id}")
        seen.add(s.id)

    if ids:
        scenarios = [s for s in scenarios if s.id in ids]
    if tags:
        scenarios = [s for s in scenarios if set(tags) & set(s.tags)]
    return scenarios
