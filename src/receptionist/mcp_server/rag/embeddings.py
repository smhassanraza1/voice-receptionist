"""Optional dense ranker. Off by default; enabled with `RAG_DENSE=fastembed|openai`.

Both backends are imported lazily and any failure (package missing, no API key, no network to
download a model) disables dense retrieval with a warning instead of breaking the server.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Sequence
from typing import Protocol

logger = logging.getLogger(__name__)

FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
OPENAI_MODEL = "text-embedding-3-small"


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    def __init__(self, model: str = FASTEMBED_MODEL) -> None:
        from fastembed import TextEmbedding  # optional dependency (`dense` extra)

        self._model = TextEmbedding(model_name=model)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(list(texts))]


class OpenAIEmbedder:
    def __init__(self, model: str = OPENAI_MODEL) -> None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set")
        from openai import OpenAI

        self._client = OpenAI(timeout=10)
        self._model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.embeddings.create(model=self._model, input=list(texts))
        return [item.embedding for item in response.data]


class DenseRanker:
    """Cosine similarity between the query embedding and precomputed chunk embeddings."""

    name = "dense"

    def __init__(self, embedder: Embedder, documents: Sequence[str]) -> None:
        self._embedder = embedder
        self._vectors = [_unit(v) for v in embedder.embed(documents)]

    def rank(self, query: str, top_n: int) -> list[tuple[int, float]]:
        (query_vector,) = self._embedder.embed([query])
        q = _unit(query_vector)
        scores = [
            (i, sum(a * b for a, b in zip(q, v, strict=True))) for i, v in enumerate(self._vectors)
        ]
        return sorted(scores, key=lambda item: (-item[1], item[0]))[:top_n]


def load_dense_ranker(backend: str, documents: Sequence[str]) -> DenseRanker | None:
    """Build the configured dense ranker, or return None (logged) when it is unavailable."""
    if backend == "none":
        return None
    factories = {"fastembed": FastEmbedEmbedder, "openai": OpenAIEmbedder}
    if backend not in factories:
        raise ValueError(f"unknown dense backend {backend!r}")
    try:
        return DenseRanker(factories[backend](), documents)
    except Exception as exc:  # optional component: degrade to lexical-only retrieval
        logger.warning("Dense retrieval (%s) unavailable, using lexical only: %s", backend, exc)
        return None


def _unit(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]
