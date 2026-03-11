"""SPLADE sparse neural retrieval engine.

Uses fastembed's SparseTextEmbedding for ONNX-based SPLADE encoding.
Stores sparse vectors in-memory for brute-force dot-product search.
Suitable for corpora up to ~100K memories.

Disabled by default; enable via config.splade_enabled = True.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ncms.domain.models import Memory

logger = logging.getLogger(__name__)


@dataclass
class SparseVector:
    """A sparse vector: parallel arrays of vocabulary indices and weights."""

    indices: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)


class SpladeEngine:
    """SPLADE sparse neural retrieval engine.

    Encodes text into sparse vectors via fastembed's ONNX-based SPLADE model.
    Stores vectors in-memory (``dict[str, SparseVector]``) and performs
    brute-force dot-product search.  Suitable for corpora up to ~100K memories.
    """

    def __init__(
        self,
        model_name: str = "prithivida/Splade_PP_en_v1",
        cache_dir: str | None = None,
    ):
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: object | None = None  # Lazy-loaded SparseTextEmbedding
        self._vectors: dict[str, SparseVector] = {}

    def _ensure_model(self) -> None:
        """Lazy-load the SPLADE model on first use (~530 MB ONNX download)."""
        if self._model is not None:
            return
        from fastembed import SparseTextEmbedding

        kwargs: dict[str, object] = {"model_name": self._model_name}
        if self._cache_dir:
            kwargs["cache_dir"] = self._cache_dir
        self._model = SparseTextEmbedding(**kwargs)
        logger.info("SPLADE model loaded: %s", self._model_name)

    def index_memory(self, memory: Memory) -> None:
        """Encode a memory's content and store its sparse vector."""
        self._ensure_model()
        embeddings = list(self._model.embed([memory.content], batch_size=1))  # type: ignore[union-attr]
        if embeddings:
            emb = embeddings[0]
            self._vectors[memory.id] = SparseVector(
                indices=emb.indices.tolist(),
                values=emb.values.tolist(),
            )

    def remove(self, memory_id: str) -> None:
        """Remove a memory's sparse vector from the store."""
        self._vectors.pop(memory_id, None)

    def search(self, query: str, limit: int = 50) -> list[tuple[str, float]]:
        """Search by SPLADE sparse dot-product similarity.

        Returns ``(memory_id, splade_score)`` pairs sorted descending by score.
        """
        if not self._vectors:
            return []

        self._ensure_model()
        query_embeddings = list(self._model.embed([query], batch_size=1))  # type: ignore[union-attr]
        if not query_embeddings:
            return []

        q_emb = query_embeddings[0]
        q_map: dict[int, float] = dict(
            zip(q_emb.indices.tolist(), q_emb.values.tolist(), strict=True)
        )

        scores: list[tuple[str, float]] = []
        for memory_id, sv in self._vectors.items():
            dot = 0.0
            for idx, val in zip(sv.indices, sv.values, strict=True):
                if idx in q_map:
                    dot += val * q_map[idx]
            if dot > 0.0:
                scores.append((memory_id, dot))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:limit]

    @property
    def vector_count(self) -> int:
        """Number of stored sparse vectors."""
        return len(self._vectors)
