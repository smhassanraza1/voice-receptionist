"""Pushes eval results to Langfuse: one trace per scenario run, scored, grouped by run id.

Each trace carries the transcript, the scores and the judge's rationale, and the run id is its
Langfuse session. These traces stand on their own: text runs drive the agent in-process
without its telemetry, and on voice runs the agent's OpenTelemetry spans (exported by
`receptionist.agent.telemetry`) are grouped under the LiveKit room name, not the run id.
"""

from __future__ import annotations

import logging
import os

from .results import ScenarioResult
from .scoring import format_transcript
from .stats import percentile

logger = logging.getLogger("receptionist.evals")


def langfuse_enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def export(results: list[ScenarioResult], *, run_id: str) -> None:
    if not langfuse_enabled() or not results:
        return
    from langfuse import get_client, propagate_attributes

    client = get_client()
    for r in results:
        with (
            client.start_as_current_observation(
                name=f"eval/{r.scenario_id}",
                input={"scenario": r.scenario_id, "arch": r.arch, "channel": r.channel},
                metadata={"model": r.model, "run_id": run_id},
            ) as span,
            propagate_attributes(session_id=run_id, tags=[r.arch, r.channel, "eval"]),
        ):
            span.update(output=format_transcript(r.events))
            span.score_trace(name="passed", value=1.0 if r.passed else 0.0, data_type="BOOLEAN")
            for name, value in (
                ("tool_recall", r.tools.recall),
                ("tool_arg_accuracy", r.tools.arg_accuracy),
            ):
                if value is not None:  # not applicable to this scenario
                    span.score_trace(name=name, value=value)
            if r.verdict:
                for dim, value in r.verdict.to_dict().items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        span.score_trace(
                            name=f"judge_{dim}", value=float(value), comment=r.verdict.rationale
                        )
            for name, values in r.latency_ms.items():
                if (p50 := percentile(values, 0.5)) is not None:
                    span.score_trace(name=f"{name}_p50_ms", value=p50)
    client.flush()
    logger.info("exported %d eval traces to Langfuse (session %s)", len(results), run_id)
