"""Eval CLI.

    python -m receptionist.evals text   --arch pipeline realtime
    python -m receptionist.evals voice  --arch pipeline --ids new-patient-dermatology
    python -m receptionist.evals probes --arch pipeline realtime --calls 5
    python -m receptionist.evals report evals/results/<run>

Results land in evals/results/<run-id>/ (results.jsonl, probes.json, summary.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from receptionist.agent.settings import AgentSettings

from .architectures import ARCHITECTURES
from .langfuse_export import export
from .report import write_report
from .results import ScenarioResult
from .scenarios import load_scenarios
from .services import EvalServices

load_dotenv()
logger = logging.getLogger("receptionist.evals")

SIMULATOR_MODEL = os.getenv("EVAL_SIMULATOR_MODEL", "gpt-4.1-mini")
JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", "gpt-5-mini")


def _llm(model: str):
    from livekit.plugins import openai

    return openai.LLM(model=model)


def _run_dir(args: argparse.Namespace, channel: str) -> Path:
    run_id = args.run_id or f"{datetime.now():%Y%m%d-%H%M%S}-{channel}"
    path = Path(args.out) / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append(run_dir: Path, result: ScenarioResult) -> None:
    with (run_dir / "results.jsonl").open("a") as f:
        f.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
    status = "PASS" if result.passed else "FAIL"
    print(
        f"  {status}  {result.arch:<16} {result.scenario_id}"
        + (f"  ({result.error})" if result.error else "")
    )


def _print_spend(results: list[ScenarioResult]) -> None:
    """What the run cost in API usage: the agents under test plus the simulator and judge."""
    agent = [r.usage.get("cost") or {} for r in results]
    overhead = [(r.usage.get("eval_overhead") or {}).get("cost") or {} for r in results]
    agent_usd = sum(c.get("total_usd", 0.0) for c in agent)
    overhead_usd = sum(c.get("total_usd", 0.0) for c in overhead)
    unpriced = sorted({m for c in agent + overhead for m in c.get("unpriced", [])})
    print(
        f"\nspend: ${agent_usd + overhead_usd:.4f} "
        f"(agents ${agent_usd:.4f} + simulator/judge ${overhead_usd:.4f})"
        + (f"; not priced: {', '.join(unpriced)}" if unpriced else "")
    )


async def cmd_text(args: argparse.Namespace) -> Path:
    from .text_runner import run_text_scenario

    run_dir = _run_dir(args, "text")
    scenarios = load_scenarios(Path(args.scenarios), ids=args.ids, tags=args.tags)
    settings = AgentSettings()
    judge = None if args.no_judge else _llm(args.judge_model)
    results: list[ScenarioResult] = []
    with EvalServices(Path(tempfile.mkdtemp(prefix="eval-"))) as services:
        for arch in args.arch:
            for scenario in scenarios:
                result = await run_text_scenario(
                    scenario,
                    arch,
                    services,
                    base_settings=settings,
                    simulator_llm=_llm(args.sim_model),
                    judge_llm=judge,
                )
                results.append(result)
                _append(run_dir, result)
    _print_spend(results)
    export(results, run_id=run_dir.name)
    return write_report(run_dir)


async def cmd_voice(args: argparse.Namespace) -> Path:
    from .voice.runner import run_voice_scenario
    from .voice.stack import AgentWorker, LocalLiveKit

    run_dir = _run_dir(args, "voice")
    scenarios = load_scenarios(Path(args.scenarios), ids=args.ids, tags=args.tags)
    settings = AgentSettings()
    judge = None if args.no_judge else _llm(args.judge_model)
    results: list[ScenarioResult] = []
    workdir = Path(tempfile.mkdtemp(prefix="eval-voice-"))
    with (
        EvalServices(workdir) as services,
        LocalLiveKit(workdir) as livekit,
        AgentWorker(livekit, services, workdir) as worker,
    ):
        for arch in args.arch:
            for scenario in scenarios:
                result = await run_voice_scenario(
                    scenario,
                    arch,
                    services=services,
                    livekit=livekit,
                    worker=worker,
                    settings=settings,
                    simulator_llm=_llm(args.sim_model),
                    judge_llm=judge,
                )
                results.append(result)
                _append(run_dir, result)
    _print_spend(results)
    export(results, run_id=run_dir.name)
    return write_report(run_dir)


async def cmd_probes(args: argparse.Namespace) -> Path:
    from .voice.probes import load_probes, summarize
    from .voice.runner import run_probe_calls
    from .voice.stack import AgentWorker, LocalLiveKit

    run_dir = _run_dir(args, "probes")
    probes = load_probes(Path(args.probes))
    workdir = Path(tempfile.mkdtemp(prefix="eval-probes-"))
    summaries, raw = {}, {}
    with (
        EvalServices(workdir) as services,
        LocalLiveKit(workdir) as livekit,
        AgentWorker(livekit, services, workdir),
    ):
        for arch in args.arch:
            runs = await run_probe_calls(arch, probes, livekit=livekit, calls=args.calls)
            summaries[arch] = summarize(runs)
            raw[arch] = [r.to_dict() for r in runs]
            print(f"  {arch}: {json.dumps(summaries[arch])}")
    (run_dir / "probes.json").write_text(json.dumps(summaries, indent=2))
    (run_dir / "probes-raw.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    (run_dir / "results.jsonl").touch()
    return write_report(run_dir)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m receptionist.evals")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, scenarios: bool = True) -> None:
        p.add_argument("--arch", nargs="+", choices=ARCHITECTURES, default=["pipeline"])
        p.add_argument("--out", default="evals/results")
        p.add_argument("--run-id")
        if scenarios:
            p.add_argument("--scenarios", default="evals/scenarios")
            p.add_argument("--ids", nargs="*")
            p.add_argument("--tags", nargs="*")
            p.add_argument("--sim-model", default=SIMULATOR_MODEL)
            p.add_argument("--judge-model", default=JUDGE_MODEL)
            p.add_argument("--no-judge", action="store_true")

    common(sub.add_parser("text", help="simulated callers over text (fast, cheap)"))
    common(sub.add_parser("voice", help="simulated callers over real audio via LiveKit"))
    probes = sub.add_parser("probes", help="scripted latency / barge-in / backchannel probes")
    common(probes, scenarios=False)
    probes.add_argument("--probes", default="evals/probes.yaml")
    probes.add_argument("--calls", type=int, default=3)
    report = sub.add_parser("report", help="rebuild summary.md for a run directory")
    report.add_argument("run_dir")

    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("EVAL_LOG_LEVEL", "WARNING"))

    if args.command == "report":
        out = write_report(Path(args.run_dir))
    else:
        out = asyncio.run(
            {"text": cmd_text, "voice": cmd_voice, "probes": cmd_probes}[args.command](args)
        )
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
