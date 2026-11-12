from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .judge import Verdict
from .outcomes import OutcomeResult
from .scoring import Event, ToolScore


@dataclass
class ScenarioResult:
    scenario_id: str
    arch: str
    channel: str  # "text" | "voice"
    model: str
    events: list[Event]
    tools: ToolScore
    outcomes: list[OutcomeResult]
    handoff_ok: bool
    verdict: Verdict | None = None
    judge_error: str | None = None
    latency_ms: dict[str, list[float]] = field(default_factory=dict)
    interruption: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    error: str | None = None

    @property
    def outcomes_ok(self) -> bool:
        return all(o.passed for o in self.outcomes)

    @property
    def passed(self) -> bool:
        if self.error or self.judge_error:  # an ungraded run is not a pass
            return False
        checks = self.outcomes_ok and self.handoff_ok and not self.tools.forbidden_violations
        return checks and (self.verdict.passed if self.verdict else True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "arch": self.arch,
            "channel": self.channel,
            "model": self.model,
            "passed": self.passed,
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
            "turns": sum(1 for e in self.events if e.role == "caller"),
            "tools": self.tools.to_dict(),
            "outcomes": [
                {"kind": o.outcome.kind, "passed": o.passed, "detail": o.detail}
                for o in self.outcomes
            ],
            "handoff_ok": self.handoff_ok,
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "judge_error": self.judge_error,
            "latency_ms": self.latency_ms,
            "interruption": self.interruption,
            "usage": self.usage,
            "transcript": [asdict(e) for e in self.events],
        }
