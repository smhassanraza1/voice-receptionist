"""Hybrid retrieval: several rankers fused with Reciprocal Rank Fusion, plus a confidence gate.

RRF (Cormack et al., 2009) combines rankings, not scores, so rankers with incomparable score
scales (BM25, cosine) fuse without calibration. The flip side is that a fused score says how
much the rankers *agree*, not whether anything is relevant: every query, even "tell me a joke",
has a top result. Relevance is therefore judged separately by `TermCoverage` on the best
candidates, and below `min_confidence` the retriever reports no confident match.

The gate is lexical: with a dense ranker enabled, paraphrases that share no words with the
knowledge base can rank well yet still be gated out. Re-tune `min_confidence` with
`python -m receptionist.mcp_server.rag.evaluate --dense ...` before relying on it.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .chunking import Chunk, chunk_documents, load_documents
from .embeddings import load_dense_ranker
from .lexical import BM25Ranker, CharNgramRanker, TermCoverage

logger = logging.getLogger(__name__)

# "bm25_exact" is BM25 without typo-tolerant expansion, kept for the evaluation ablation.
RankerName = Literal["bm25", "bm25_exact", "char_ngram", "dense"]
LEXICAL_RANKERS: tuple[RankerName, ...] = ("bm25", "char_ngram")
DEFAULT_MIN_CONFIDENCE = 0.4


class Ranker(Protocol):
    name: str

    def rank(self, query: str, top_n: int) -> list[tuple[int, float]]:
        """(chunk index, score) pairs, best first."""
        ...


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float  # normalised fused score: 1.0 means every ranker put this chunk first


@dataclass(frozen=True)
class SearchResult:
    hits: list[Hit]  # ranked even when not confident, so evaluation can inspect them
    confidence: float
    confident: bool


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[tuple[int, float]]], k: int = 60
) -> list[tuple[int, float]]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, (index, _score) in enumerate(ranking, start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))


class Retriever:
    def __init__(
        self,
        chunks: Sequence[Chunk],
        rankers: Sequence[Ranker],
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        rrf_k: int = 60,
        pool_size: int = 20,
        confidence_depth: int = 3,
    ) -> None:
        if not rankers:
            raise ValueError("at least one ranker is required")
        self.chunks = list(chunks)
        self.rankers = list(rankers)
        self.min_confidence = min_confidence
        self._rrf_k = rrf_k
        self._pool_size = pool_size
        self._confidence_depth = confidence_depth
        self._coverage = TermCoverage([c.index_text for c in self.chunks])

    @property
    def ranker_names(self) -> list[str]:
        return [r.name for r in self.rankers]

    def search(self, query: str, top_k: int = 4) -> SearchResult:
        rankings = [ranker.rank(query, self._pool_size) for ranker in self.rankers]
        fused = reciprocal_rank_fusion(rankings, self._rrf_k)
        best_possible = len(self.rankers) / (self._rrf_k + 1)
        hits = [Hit(self.chunks[i], round(score / best_possible, 4)) for i, score in fused[:top_k]]
        confidence = max(
            (self._coverage(query, i) for i, _ in fused[: self._confidence_depth]), default=0.0
        )
        return SearchResult(hits, round(confidence, 4), confidence >= self.min_confidence)


def build_retriever(
    kb_dir: str | Path,
    *,
    rankers: Sequence[RankerName] | None = None,
    dense_backend: str = "none",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> Retriever:
    """Chunk the knowledge base and index it.

    `rankers` defaults to the lexical rankers, plus `dense` when a `dense_backend` is set. A
    requested `dense` ranker is skipped (with a warning) when its backend is unavailable.
    """
    if rankers is None:
        rankers = (*LEXICAL_RANKERS, "dense") if dense_backend != "none" else LEXICAL_RANKERS
    chunks = chunk_documents(load_documents(kb_dir))
    texts = [c.index_text for c in chunks]
    built = [r for name in rankers if (r := _make_ranker(name, texts, dense_backend)) is not None]
    logger.info("Indexed %d chunks from %s with %s", len(chunks), kb_dir, [r.name for r in built])
    return Retriever(chunks, built, min_confidence=min_confidence)


def _make_ranker(name: RankerName, texts: list[str], dense_backend: str) -> Ranker | None:
    match name:
        case "bm25":
            return BM25Ranker(texts)
        case "bm25_exact":
            return BM25Ranker(texts, fuzzy_min_similarity=None)
        case "char_ngram":
            return CharNgramRanker(texts)
        case "dense":
            return load_dense_ranker(dense_backend, texts)


@functools.cache
def load_retriever(
    kb_dir: Path, dense_backend: str = "none", min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> Retriever:
    """Process-wide cached index (built once at server start-up)."""
    return build_retriever(kb_dir, dense_backend=dense_backend, min_confidence=min_confidence)
