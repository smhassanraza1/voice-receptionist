from receptionist.evals.langfuse_export import percentile as langfuse_percentile
from receptionist.evals.report import percentile as report_percentile
from receptionist.evals.stats import percentile
from receptionist.evals.voice.probes import ProbeResult, ProbeRun, summarize


def test_nearest_rank_percentiles():
    ten = [float(v) for v in range(1, 11)]
    assert percentile(ten, 0.9) == 9.0  # the 9th of 10, not the maximum
    assert percentile(ten, 0.5) == 5.0
    assert percentile([300.0, 500.0], 0.5) == 300.0
    assert percentile([42.0], 0.9) == 42.0
    assert percentile([], 0.5) is None
    assert percentile([3.0, 1.0, 2.0], 1.0) == 3.0


def test_every_summary_uses_the_shared_helper():
    assert report_percentile is percentile and langfuse_percentile is percentile
    runs = [
        ProbeRun(
            arch="pipeline",
            greeting_ttfa_ms=None,
            results=[ProbeResult(probe_id=str(i), ttfa_ms=float(i)) for i in range(1, 11)],
        )
    ]
    assert summarize(runs)["ttfa_ms"]["p90"] == 9.0
