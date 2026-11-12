import json

from receptionist.evals.report import aggregate, to_markdown, write_report


def row(arch: str, sid: str, passed: bool, **extra) -> dict:
    return {
        "scenario_id": sid,
        "arch": arch,
        "channel": "text",
        "model": f"model-{arch}",
        "passed": passed,
        "error": None,
        "turns": 4,
        "tools": {
            "recall": 1.0 if passed else 0.5,
            "arg_accuracy": 1.0,
            "write_precision": 1.0,
            "forbidden_violations": [],
        },
        "outcomes": [{"kind": "booked", "passed": passed, "detail": ""}],
        "handoff_ok": True,
        "verdict": {
            "task_success": 5 if passed else 2,
            "policy_adherence": 5,
            "faithfulness": 4,
            "voice_style": 4,
            "language": 5,
            "passed": passed,
            "rationale": "",
        },
        "latency_ms": {"llm_ttft": [300.0, 500.0]},
        **extra,
    }


def test_aggregate_and_markdown(tmp_path):
    rows = [
        row("pipeline", "a", True),
        row("pipeline", "b", False),
        row("realtime", "a", True),
    ]
    summary = aggregate(rows)
    pipe = summary["text/pipeline"]
    assert pipe["pass_rate"] == 0.5
    assert pipe["outcome_accuracy"] == 0.5
    assert pipe["tool_recall"] == 0.75
    assert pipe["judge"]["task_success"] == 3.5
    assert pipe["latency_ms"]["llm_ttft"] == {"p50": 300.0, "p90": 500.0, "n": 4}

    md = to_markdown(rows, summary)
    assert "| **pass rate** | 50% | 100% |" in md
    assert "| b | ❌ | – |" in md

    (tmp_path / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    out = write_report(tmp_path)
    assert out.read_text() == md
    assert (
        json.loads((tmp_path / "summary.json").read_text())["scenarios"]["text/pipeline"][
            "scenarios"
        ]
        == 2
    )


def test_not_applicable_metrics_are_left_out_of_the_means():
    applies = row("pipeline", "a", True)
    vacuous = row("pipeline", "b", True)
    vacuous["tools"] = {
        "recall": None,
        "arg_accuracy": None,
        "write_precision": None,
        "forbidden_violations": [],
    }
    partial = row("pipeline", "c", False)
    partial["tools"]["arg_accuracy"] = 0.5
    summary = aggregate([applies, vacuous, partial])["text/pipeline"]
    assert summary["tool_recall"] == 0.75
    assert summary["tool_arg_accuracy"] == 0.75
    only_vacuous = aggregate([vacuous])["text/pipeline"]
    assert only_vacuous["tool_recall"] is None
    assert "| tool-call recall | – |" in to_markdown([vacuous], {"text/pipeline": only_vacuous})


def test_judge_errors_are_counted_and_listed():
    failed = row("pipeline", "b", False, verdict=None, judge_error="APIError: rate limited")
    rows = [row("pipeline", "a", True), failed]
    summary = aggregate(rows)
    assert summary["text/pipeline"]["judge_errors"] == 1
    md = to_markdown(rows, summary)
    assert "| judge errors | 1 |" in md
    assert "| text/pipeline b | judge: APIError: rate limited |" in md


def test_measured_cost_and_eval_overhead():
    def usage(agent: float, overhead: float) -> dict:
        return {
            "cost": {"total_usd": agent, "unpriced": []},
            "eval_overhead": {"cost": {"total_usd": overhead, "unpriced": ["acme/x"]}},
        }

    rows = [
        row("pipeline", "a", True, usage=usage(0.01, 0.002)),
        row("pipeline", "b", True, usage=usage(0.03, 0.004)),
    ]
    pipe = aggregate(rows)["text/pipeline"]
    assert pipe["cost_per_call_usd"] == 0.02
    assert pipe["eval_overhead_per_call_usd"] == 0.003
    assert pipe["spend_usd"] == 0.046
    assert pipe["unpriced_models"] == ["acme/x"]
    md = to_markdown(rows, {"text/pipeline": pipe})
    assert "| eval overhead / call (USD) | 0.003 |" in md
    assert "| run spend, agent + eval (USD) | 0.046 |" in md


def test_a_probes_only_run_reports_just_the_probes():
    probe = {
        "ttfa_ms": {"p50": 900.0, "p90": 1200.0},
        "barge_in": {"stop_rate": 1.0, "stop_latency_p50_ms": 300.0},
        "backchannel": {"false_interruption_rate": 0.0},
    }
    md = to_markdown([], {}, probes={"pipeline": probe})
    assert md.splitlines()[0] == "| voice probes | pipeline |"
    assert "| TTFA p50 (ms) | 900 |" in md and "pass rate" not in md
