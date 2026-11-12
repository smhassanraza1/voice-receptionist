"""Grades a finished call the same way for both channels."""

from __future__ import annotations

import logging
from typing import Any

from livekit.agents import llm

from . import outcomes as outcome_checks
from .judge import Verdict, judge_call
from .outcomes import OutcomeResult
from .results import ScenarioResult
from .scenarios import Scenario
from .scoring import Event, format_transcript, handoff_ok, score_tool_calls
from .services import EvalServices

logger = logging.getLogger("receptionist.evals")


async def snapshot_outcomes(
    services: EvalServices, scenario: Scenario
) -> list[list[dict[str, Any]]]:
    return [await outcome_checks.snapshot(services, o) for o in scenario.expected.outcomes]


async def grade(
    scenario: Scenario,
    events: list[Event],
    *,
    services: EvalServices,
    befores: list[list[dict[str, Any]]],
    judge_llm: llm.LLM | None,
    arch: str,
    channel: str,
    model: str,
    duration_s: float,
    error: str | None = None,
    latency_ms: dict[str, list[float]] | None = None,
    interruption: dict[str, Any] | None = None,
) -> ScenarioResult:
    """Never raises: a grading or judge failure is recorded on the result, so one bad
    scenario can't stop the suite or the report."""
    graded: list[OutcomeResult] = []
    try:
        afters = await snapshot_outcomes(services, scenario)
        graded = [
            outcome_checks.evaluate(o, before, after)
            for o, before, after in zip(scenario.expected.outcomes, befores, afters, strict=True)
        ]
    except Exception as e:
        logger.exception("final-state grading failed for %s", scenario.id)
        error = error or f"final-state grading failed: {type(e).__name__}: {e}"

    verdict: Verdict | None = None
    judge_error: str | None = None
    if judge_llm is not None and events and error is None:
        try:
            verdict = await judge_call(
                judge_llm,
                goal=scenario.goal,
                expectations=scenario.expectations,
                transcript=format_transcript(events),
            )
        except Exception as e:
            logger.exception("judge failed for %s", scenario.id)
            judge_error = f"{type(e).__name__}: {e}"

    return ScenarioResult(
        scenario_id=scenario.id,
        arch=arch,
        channel=channel,
        model=model,
        events=events,
        tools=score_tool_calls(scenario.expected, events),
        outcomes=graded,
        handoff_ok=handoff_ok(scenario.expected, events),
        verdict=verdict,
        judge_error=judge_error,
        latency_ms=latency_ms or {},
        interruption=interruption or {},
        duration_s=duration_s,
        error=error,
    )
