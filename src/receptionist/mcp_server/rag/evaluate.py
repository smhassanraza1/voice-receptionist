"""Evaluate retrieval on the golden set and write `docs/results/rag_eval.json`.

    python -m receptionist.mcp_server.rag.evaluate [--dense fastembed|openai]

Ranking metrics (recall@k, MRR) are computed at document level over in-scope queries and
ignore the confidence gate; the gate is scored separately (out-of-scope rejection rate and the
in-scope false-rejection rate), so a strict threshold cannot hide behind good recall.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..settings import DEFAULT_KB_DIR, REPO_ROOT
from .retriever import DEFAULT_MIN_CONFIDENCE, RankerName, Retriever, build_retriever

GOLDEN_SET = Path(__file__).with_name("golden_set.yaml")
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "results" / "rag_eval.json"
SEARCH_DEPTH = 20

METHOD = {
    "chunking": "by markdown section; sections under 60 words merged, over 150 split (25-word overlap)",
    "bm25_exact": "Okapi BM25 (k1=1.2, b=0.75) over stemmed content words",
    "bm25": "bm25_exact plus joined-neighbour and nearest-term (trigram Dice >= 0.6) expansion",
    "char_ngram": "TF-IDF cosine over 3-5 character n-grams within words",
    "fusion": "Reciprocal Rank Fusion, k=60, over each ranker's top 20 chunks",
    "confidence": "length-weighted soft term coverage of the best of the top 3 fused chunks",
    "metrics": "document-level; recall/MRR ignore the confidence gate, which is scored separately",
}

CONFIGURATIONS: dict[str, tuple[RankerName, ...]] = {
    "bm25_exact": ("bm25_exact",),
    "bm25": ("bm25",),
    "char_ngram": ("char_ngram",),
    "fused": ("bm25", "char_ngram"),
    "fused+dense": ("bm25", "char_ngram", "dense"),
}


@dataclass(frozen=True)
class Case:
    query: str
    relevant: tuple[str, ...]
    kind: str

    @property
    def in_scope(self) -> bool:
        return bool(self.relevant)


def load_golden_set(path: Path = GOLDEN_SET) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Case(r["query"], tuple(r["relevant"]), r["kind"]) for r in raw]


def evaluate(retriever: Retriever, cases: Sequence[Case]) -> dict[str, Any]:
    per_query = []
    for case in cases:
        result = retriever.search(case.query, top_k=SEARCH_DEPTH)
        docs = list(dict.fromkeys(hit.chunk.doc_id for hit in result.hits))
        rank = next((i for i, d in enumerate(docs, 1) if d in case.relevant), None)
        per_query.append(
            {
                "query": case.query,
                "kind": case.kind,
                "relevant": list(case.relevant),
                "top_docs": docs[:3],
                "rank": rank,
                "confidence": result.confidence,
                "confident": result.confident,
            }
        )
    in_scope = [q for q in per_query if q["relevant"]]
    out_of_scope = [q for q in per_query if not q["relevant"]]
    correct = [
        q
        for q in per_query
        if (q["relevant"] and q["confident"] and q["rank"] and q["rank"] <= 3)
        or (not q["relevant"] and not q["confident"])
    ]
    return {
        "metrics": {
            "recall@1": _share(in_scope, lambda q: q["rank"] == 1),
            "recall@3": _share(in_scope, lambda q: q["rank"] is not None and q["rank"] <= 3),
            "mrr": round(sum(1 / q["rank"] for q in in_scope if q["rank"]) / len(in_scope), 3),
            "out_of_scope_rejection_rate": _share(out_of_scope, lambda q: not q["confident"]),
            "in_scope_false_rejection_rate": _share(in_scope, lambda q: not q["confident"]),
            "end_to_end_accuracy": round(len(correct) / len(per_query), 3),
        },
        "recall@3_by_kind": {
            kind: _share(group, lambda q: q["rank"] is not None and q["rank"] <= 3)
            for kind in sorted({q["kind"] for q in in_scope})
            if (group := [q for q in in_scope if q["kind"] == kind])
        },
        "queries": per_query,
    }


def run(
    kb_dir: Path,
    golden_set: Path,
    *,
    dense_backend: str = "none",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    cases = load_golden_set(golden_set)
    shown_path = (
        golden_set.relative_to(REPO_ROOT) if golden_set.is_relative_to(REPO_ROOT) else golden_set
    )
    report: dict[str, Any] = {
        "golden_set": {
            "path": str(shown_path),
            "in_scope": sum(c.in_scope for c in cases),
            "out_of_scope": sum(not c.in_scope for c in cases),
        },
        "method": METHOD,
        "min_confidence": min_confidence,
        "configurations": {},
    }
    for name, rankers in CONFIGURATIONS.items():
        retriever = build_retriever(
            kb_dir, rankers=rankers, dense_backend=dense_backend, min_confidence=min_confidence
        )
        if retriever.ranker_names != list(rankers):
            report["configurations"][name] = {"skipped": "dense backend unavailable"}
            continue
        report["configurations"][name] = {
            "rankers": retriever.ranker_names,
            "chunks": len(retriever.chunks),
            **evaluate(retriever, cases),
        }
    return report


_COLUMNS = {
    "recall@1": "R@1",
    "recall@3": "R@3",
    "mrr": "MRR",
    "out_of_scope_rejection_rate": "OOS-rej",
    "in_scope_false_rejection_rate": "false-rej",
    "end_to_end_accuracy": "e2e-acc",
}


def print_summary(report: dict[str, Any]) -> None:
    print(f"{'configuration':<14}" + "".join(f"{label:>10}" for label in _COLUMNS.values()))
    for name, config in report["configurations"].items():
        if "metrics" not in config:
            print(f"{name:<14}  ({config['skipped']})")
            continue
        print(f"{name:<14}" + "".join(f"{config['metrics'][c]:>10.3f}" for c in _COLUMNS))
    print(f"\nfused, R@3 by kind: {report['configurations']['fused']['recall@3_by_kind']}")
    for q in report["configurations"]["fused"]["queries"]:
        if q["relevant"] and (q["rank"] or 99) > 3:
            print(f"  miss@3    {q['query']!r} -> {q['top_docs']}")
        elif bool(q["relevant"]) != q["confident"]:
            label = "accepted" if q["confident"] else "rejected"
            print(f"  {label:<9} {q['query']!r} (confidence {q['confidence']})")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kb-dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--golden-set", type=Path, default=GOLDEN_SET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dense", choices=["none", "fastembed", "openai"], default="none")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    args = parser.parse_args(argv)

    report = run(
        args.kb_dir, args.golden_set, dense_backend=args.dense, min_confidence=args.min_confidence
    )
    print_summary(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")


def _share(items: list[dict[str, Any]], predicate: Any) -> float:
    return round(sum(1 for q in items if predicate(q)) / len(items), 3) if items else 0.0


if __name__ == "__main__":
    main()
