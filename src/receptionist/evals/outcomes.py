"""Final-state grading (tau-bench style): did the clinic's records end up right?

Grading goes through the MCP server's own `lookup_appointments` tool, so it checks what a
real staff member would see, independent of the database schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .scenarios import Outcome
from .services import EvalServices

GRADER = "eval-grader"


@dataclass
class OutcomeResult:
    outcome: Outcome
    passed: bool
    detail: str


def _lookup_args(o: Outcome) -> dict[str, Any]:
    args: dict[str, Any] = {"date_of_birth": o.date_of_birth}
    if o.confirmation_code:
        args["confirmation_code"] = o.confirmation_code
    elif o.phone:
        args["phone"] = o.phone
    return args


async def snapshot(services: EvalServices, outcome: Outcome) -> list[dict[str, Any]]:
    ok, payload = await services.call_tool(
        "lookup_appointments", _lookup_args(outcome), subject=GRADER
    )
    return payload.get("appointments", []) if ok and isinstance(payload, dict) else []


def _matches(appt: dict[str, Any], o: Outcome) -> bool:
    return o.specialty is None or appt.get("specialty") == o.specialty


def evaluate(
    o: Outcome, before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> OutcomeResult:
    before_codes = {a["confirmation_code"]: a for a in before}
    after_codes = {a["confirmation_code"]: a for a in after}

    if o.kind == "booked":
        new = [
            a
            for code, a in after_codes.items()
            if code not in before_codes and a["status"] == "booked"
        ]
        hits = [a for a in new if _matches(a, o)]
        if len(hits) == 1:
            return OutcomeResult(
                o, True, f"booked {hits[0]['specialty']} on {hits[0]['local_date']}"
            )
        return OutcomeResult(
            o, False, f"expected one new matching booking, found {len(hits)} (new: {len(new)})"
        )

    if o.kind == "no_booking":
        new = [code for code in after_codes if code not in before_codes]
        return OutcomeResult(
            o, not new, "no new bookings" if not new else f"unexpected bookings {new}"
        )

    code = o.confirmation_code or ""
    prior, current = before_codes.get(code), after_codes.get(code)
    if current is None:
        return OutcomeResult(o, False, f"{code} not found after the call")

    if o.kind == "cancelled":
        ok = current["status"] == "cancelled"
        return OutcomeResult(o, ok, f"{code} status={current['status']}")
    if o.kind == "rescheduled":
        ok = (
            current["status"] == "booked"
            and prior is not None
            and current["start"] != prior["start"]
        )
        return OutcomeResult(
            o, ok, f"{code} moved {prior and prior['start']} -> {current['start']}"
        )
    # unchanged
    ok = (
        prior is not None
        and current["status"] == prior["status"]
        and current["start"] == prior["start"]
    )
    return OutcomeResult(o, ok, f"{code} {'unchanged' if ok else 'was modified'}")
