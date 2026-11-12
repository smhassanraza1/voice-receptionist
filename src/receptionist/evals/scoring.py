"""Deterministic scoring: tool-call accuracy and transcript checks. No LLMs here."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .scenarios import WRITE_TOOLS, Expected, ExpectedCall

Role = Literal["caller", "agent", "tool_call", "tool_result", "handoff"]


@dataclass
class Event:
    role: Role
    text: str = ""
    name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None  # pairs a tool call with its result (text channel)
    at: float = 0.0  # wall clock; orders voice-channel events captured in two processes


@dataclass
class ToolScore:
    """Tool-call accuracy for one call. A ratio is None when it doesn't apply (nothing expected,
    the tool never called, no writes made), so aggregates average only the calls it applies to.
    """

    expected: int
    matched: int
    name_matched: int
    write_calls: int
    correct_writes: int
    forbidden_violations: list[str]
    missing: list[str]
    unexpected_writes: list[str]

    @property
    def recall(self) -> float | None:
        return self.matched / self.expected if self.expected else None

    @property
    def arg_accuracy(self) -> float | None:
        return self.matched / self.name_matched if self.name_matched else None

    @property
    def write_precision(self) -> float | None:
        return self.correct_writes / self.write_calls if self.write_calls else None

    @property
    def perfect(self) -> bool:
        return (
            self.matched == self.expected
            and not self.forbidden_violations
            and not self.unexpected_writes
        )

    def to_dict(self) -> dict[str, Any]:
        def r(value: float | None) -> float | None:
            return None if value is None else round(value, 3)

        return {
            "recall": r(self.recall),
            "arg_accuracy": r(self.arg_accuracy),
            "write_precision": r(self.write_precision),
            "forbidden_violations": self.forbidden_violations,
            "missing": self.missing,
            "unexpected_writes": self.unexpected_writes,
        }


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        digits = re.sub(r"\D", "", value)
        # phone numbers: compare the last 10 digits regardless of formatting
        if len(digits) >= 10 and len(digits) >= len(value) * 0.6:
            return digits[-10:]
        return " ".join(value.casefold().split())
    return value


def args_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Every expected argument must be present with an equal (normalized) value."""
    return all(
        key in actual and _normalize(actual[key]) == _normalize(value)
        for key, value in expected.items()
    )


def _failed_calls(events: list[Event]) -> list[bool]:
    """For each tool call, in order, whether its result was an error.

    Results pair with calls by call id when the channel records one (text), otherwise with the
    earliest unanswered call of the same name (voice records each result right after its call).
    """
    calls = [e for e in events if e.role == "tool_call" and e.name]
    failed = [False] * len(calls)
    by_id = {c.call_id: i for i, c in enumerate(calls) if c.call_id}
    pending: dict[str, list[int]] = {}
    for i, c in enumerate(calls):
        if not c.call_id:
            pending.setdefault(c.name or "", []).append(i)
    for e in events:
        if e.role != "tool_result":
            continue
        if e.call_id and e.call_id in by_id:
            failed[by_id[e.call_id]] = e.is_error
        elif queue := pending.get(e.name or ""):
            failed[queue.pop(0)] = e.is_error
    return failed


def score_tool_calls(expected: Expected, events: list[Event]) -> ToolScore:
    """Match each expected call to one actual call (name, then arguments).

    A write counts as correct only when it is matched to an expected call and succeeded.
    Every other write (duplicate, wrong arguments, failed, not asked for) lowers write
    precision; unmatched writes that succeeded are also listed as unexpected.
    """
    calls = [e for e in events if e.role == "tool_call" and e.name]
    failed = _failed_calls(events)
    used: set[int] = set()
    matched = name_matched = 0
    missing: list[str] = []

    def find(exp: ExpectedCall, *, with_args: bool) -> int | None:
        hits = [
            i
            for i, call in enumerate(calls)
            if i not in used
            and call.name == exp.name
            and (not with_args or args_match(exp.args, call.args))
        ]
        # a failed attempt followed by a successful retry: the retry is the one that counts
        return next((i for i in hits if not failed[i]), hits[0] if hits else None)

    for exp in expected.tool_calls:
        if (idx := find(exp, with_args=True)) is not None:
            used.add(idx)
            matched += 1
            name_matched += 1
        elif find(exp, with_args=False) is not None:
            name_matched += 1
            missing.append(f"{exp.name}{exp.args} (called with different arguments)")
        else:
            missing.append(f"{exp.name}{exp.args or ''}")

    writes = [i for i, c in enumerate(calls) if c.name in WRITE_TOOLS]
    correct_writes = sum(1 for i in writes if i in used and not failed[i])
    unexpected = [calls[i].name or "" for i in writes if i not in used and not failed[i]]
    forbidden = [c.name for c in calls if c.name in expected.forbidden_tools]

    return ToolScore(
        expected=len(expected.tool_calls),
        matched=matched,
        name_matched=name_matched,
        write_calls=len(writes),
        correct_writes=correct_writes,
        forbidden_violations=forbidden,
        missing=missing,
        unexpected_writes=unexpected,
    )


def handoff_ok(expected: Expected, events: list[Event]) -> bool:
    targets = {e.name for e in events if e.role == "handoff"}
    targets |= {
        "billing" if e.name == "transfer_to_billing" else "human"
        for e in events
        if e.role == "tool_call" and e.name in ("transfer_to_billing", "transfer_to_human")
    }
    if expected.handoff is None:
        return "human" not in targets  # unrequested escalations are a failure
    return expected.handoff in targets


TOOL_RESULT_CHARS = 4000  # slot lists run ~1.4k characters, knowledge-base results ~2.5k
TRANSCRIPT_BUDGET_CHARS = 60_000


def format_conversation(events: list[Event]) -> str:
    """What the caller heard: caller and agent turns only."""
    return "\n".join(
        f"{'CALLER' if e.role == 'caller' else 'AGENT'}: {e.text}"
        for e in events
        if e.role in ("caller", "agent")
    )


def format_transcript(events: list[Event]) -> str:
    """The full call for the judge, tool calls and results included.

    Tool results are kept whole up to TOOL_RESULT_CHARS each, so the judge can check every
    stated fact against them. If a long call would exceed TRANSCRIPT_BUDGET_CHARS, the results
    share what the dialogue leaves of the budget evenly.
    """
    results = [e for e in events if e.role == "tool_result"]
    dialogue_chars = sum(len(e.text) + 16 for e in events if e.role != "tool_result")
    cap = TOOL_RESULT_CHARS
    if results and dialogue_chars + len(results) * cap > TRANSCRIPT_BUDGET_CHARS:
        cap = max(600, (TRANSCRIPT_BUDGET_CHARS - dialogue_chars) // len(results))

    lines = []
    for e in events:
        if e.role == "caller":
            lines.append(f"CALLER: {e.text}")
        elif e.role == "agent":
            lines.append(f"AGENT: {e.text}")
        elif e.role == "tool_call":
            lines.append(f"  [tool call] {e.name}({e.args})")
        elif e.role == "tool_result":
            prefix = "tool error" if e.is_error else "tool result"
            text = e.text if len(e.text) <= cap else e.text[:cap] + " [...truncated]"
            lines.append(f"  [{prefix}] {text}")
        elif e.role == "handoff":
            lines.append(f"  [handoff -> {e.name}]")
    return "\n".join(lines)
