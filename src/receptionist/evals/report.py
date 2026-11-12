"""Aggregates scenario results into the comparison tables."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from .stats import percentile

JUDGE_DIMS = ("task_success", "policy_adherence", "faithfulness", "voice_style", "language")


def _mean(values: list[float | None]) -> float | None:
    """Mean over the calls a metric applies to (None means not applicable)."""
    present = [v for v in values if v is not None]
    return round(statistics.mean(present), 3) if present else None


def _priced(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The agent's and the eval's own priced usage for one call (empty when not measured)."""
    usage = row.get("usage") or {}
    return usage.get("cost") or {}, (usage.get("eval_overhead") or {}).get("cost") or {}


def aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """rows are `ScenarioResult.to_dict()` records; grouped by `channel/arch`."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[f"{row['channel']}/{row['arch']}"].append(row)

    summary: dict[str, dict[str, Any]] = {}
    for key, items in sorted(groups.items()):
        verdicts = [r["verdict"] for r in items if r.get("verdict")]
        with_outcomes = [r for r in items if r["outcomes"]]
        latency: dict[str, list[float]] = defaultdict(list)
        for r in items:
            for name, values in (r.get("latency_ms") or {}).items():
                latency[name].extend(values)
        summary[key] = {
            "model": items[0]["model"],
            "scenarios": len(items),
            "pass_rate": round(sum(r["passed"] for r in items) / len(items), 3),
            "errors": sum(1 for r in items if r["error"]),
            "judge_errors": sum(1 for r in items if r.get("judge_error")),
            "outcome_accuracy": (
                round(
                    sum(all(o["passed"] for o in r["outcomes"]) for r in with_outcomes)
                    / len(with_outcomes),
                    3,
                )
                if with_outcomes
                else None
            ),
            "handoff_accuracy": round(sum(r["handoff_ok"] for r in items) / len(items), 3),
            "tool_recall": _mean([r["tools"]["recall"] for r in items]),
            "tool_arg_accuracy": _mean([r["tools"]["arg_accuracy"] for r in items]),
            "write_precision": _mean([r["tools"]["write_precision"] for r in items]),
            "forbidden_violations": sum(len(r["tools"]["forbidden_violations"]) for r in items),
            "judge": {d: _mean([v[d] for v in verdicts]) for d in JUDGE_DIMS},
            "latency_ms": {
                name: {"p50": percentile(v, 0.5), "p90": percentile(v, 0.9), "n": len(v)}
                for name, v in latency.items()
            },
            "mean_turns": _mean([r["turns"] for r in items]),
            "cost_per_call_usd": _mean([_priced(r)[0].get("total_usd") for r in items]),
            "eval_overhead_per_call_usd": _mean([_priced(r)[1].get("total_usd") for r in items]),
            "spend_usd": round(
                sum(part.get("total_usd") or 0.0 for r in items for part in _priced(r)), 5
            ),
            "unpriced_models": sorted(
                {m for r in items for part in _priced(r) for m in part.get("unpriced", [])}
            ),
        }
    return summary


def _fmt(value: Any, pct: bool = False) -> str:
    if value is None:
        return "–"
    if pct:
        return f"{value * 100:.0f}%"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _probe_table(probes: dict[str, Any]) -> list[str]:
    lines = ["| voice probes | " + " | ".join(probes) + " |", "|---|" + "---|" * len(probes)]
    probe_rows = [
        ("TTFA p50 (ms)", lambda p: p["ttfa_ms"]["p50"]),
        ("TTFA p90 (ms)", lambda p: p["ttfa_ms"]["p90"]),
        ("barge-in stop rate", lambda p: p["barge_in"]["stop_rate"]),
        ("barge-in stop latency p50 (ms)", lambda p: p["barge_in"]["stop_latency_p50_ms"]),
        (
            "backchannel false-interruption rate",
            lambda p: p["backchannel"]["false_interruption_rate"],
        ),
    ]
    for label, getter in probe_rows:
        lines.append(f"| {label} | " + " | ".join(_fmt(getter(p)) for p in probes.values()) + " |")
    return lines


def to_markdown(
    rows: list[dict[str, Any]],
    summary: dict[str, dict[str, Any]],
    probes: dict[str, Any] | None = None,
) -> str:
    if not summary:  # a probes-only run
        return "\n".join(_probe_table(probes or {})) + "\n"
    keys = list(summary)
    lines = ["| metric | " + " | ".join(keys) + " |", "|---|" + "---|" * len(keys)]

    def row(label: str, getter: Any, pct: bool = False) -> None:
        lines.append(
            f"| {label} | " + " | ".join(_fmt(getter(summary[k]), pct) for k in keys) + " |"
        )

    row("model", lambda s: s["model"])
    row("scenarios", lambda s: s["scenarios"])
    row("**pass rate**", lambda s: s["pass_rate"], pct=True)
    row("final-state accuracy", lambda s: s["outcome_accuracy"], pct=True)
    row("handoff accuracy", lambda s: s["handoff_accuracy"], pct=True)
    row("tool-call recall", lambda s: s["tool_recall"], pct=True)
    row("tool argument accuracy", lambda s: s["tool_arg_accuracy"], pct=True)
    row("write-call precision", lambda s: s["write_precision"], pct=True)
    row("forbidden tool calls", lambda s: s["forbidden_violations"])
    for dim in JUDGE_DIMS:
        row(f"judge: {dim} (1-5)", lambda s, d=dim: s["judge"][d])
    for name in sorted({n for s in summary.values() for n in s["latency_ms"]}):
        row(f"{name} p50 (ms)", lambda s, n=name: (s["latency_ms"].get(n) or {}).get("p50"))
        row(f"{name} p90 (ms)", lambda s, n=name: (s["latency_ms"].get(n) or {}).get("p90"))
    row("measured cost / call (USD)", lambda s: s.get("cost_per_call_usd"))
    row("eval overhead / call (USD)", lambda s: s.get("eval_overhead_per_call_usd"))
    row("run spend, agent + eval (USD)", lambda s: s.get("spend_usd"))
    if any(s.get("unpriced_models") for s in summary.values()):
        row("unpriced models", lambda s: ", ".join(s.get("unpriced_models") or []) or None)
    row("errors", lambda s: s["errors"])
    row("judge errors", lambda s: s.get("judge_errors"))

    scenario_ids = sorted({r["scenario_id"] for r in rows})
    by = {(f"{r['channel']}/{r['arch']}", r["scenario_id"]): r for r in rows}
    lines += ["", "| scenario | " + " | ".join(keys) + " |", "|---|" + "---|" * len(keys)]
    for sid in scenario_ids:
        cells = []
        for k in keys:
            r = by.get((k, sid))
            cells.append("–" if r is None else ("✅" if r["passed"] else "❌"))
        lines.append(f"| {sid} | " + " | ".join(cells) + " |")

    failures = [r for r in rows if r.get("error") or r.get("judge_error")]
    if failures:
        lines += ["", "| failed run | error |", "|---|---|"]
        for r in failures:
            message = f"run: {r['error']}" if r["error"] else f"judge: {r['judge_error']}"
            message = " ".join(message.split()).replace("|", "\\|")
            lines.append(f"| {r['channel']}/{r['arch']} {r['scenario_id']} | {message} |")

    if probes:
        lines += ["", *_probe_table(probes)]
    return "\n".join(lines) + "\n"


def write_report(run_dir: Path) -> Path:
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
        if line.strip()
    ]
    probes_path = run_dir / "probes.json"
    probes = json.loads(probes_path.read_text()) if probes_path.exists() else None
    summary = aggregate(rows) if rows else {}
    (run_dir / "summary.json").write_text(
        json.dumps({"scenarios": summary, "probes": probes}, indent=2)
    )
    out = run_dir / "summary.md"
    out.write_text(to_markdown(rows, summary, probes) if rows else to_markdown([], {}, probes))
    return out
