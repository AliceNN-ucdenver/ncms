"""SPLADE sparse neural retrieval engine.

Uses fastembed's SparseTextEmbedding for ONNX-based SPLADE encoding.
Stores sparse vectors in-memory for brute-force dot-product search.
Suitable for corpora up to ~100K memories.

Long texts are automatically chunked at sentence boundaries so each
chunk fits within SPLADE's 128-token window (~500 chars).  Sparse
vectors from each chunk are merged by taking the max weight per
vocabulary index, preserving the strongest signal across the document.

Disabled by default; enable via config.splade_enabled = True.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ncms.domain.models import Memory
from ncms.infrastructure.text.chunking import chunk_text

logger = logging.getLogger(__name__)

# SPLADE's tokenizer truncates at 128 tokens.  At ~4 chars/token the safe
# character budget is ~400 chars, leaving headroom for special tokens.
_SPLADE_CHUNK_MAX_CHARS: int = 400
_SPLADE_CHUNK_OVERLAP: int = 50


@dataclass
class SparseVector:
    """A sparse vector: parallel arrays of vocabulary indices and weights."""

    indices: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)


class SpladeEngine:
    """SPLADE sparse neural retrieval engine.

    Encodes text into sparse vectors via fastembed's ONNX-based SPLADE model.
    Long texts are chunked and merged (max-pool per vocab index).
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

    def _embed_chunked(self, text: str) -> SparseVector:
        """Encode text with chunking, merging via max-pool per vocab index."""
        chunks = chunk_text(text, max_chars=_SPLADE_CHUNK_MAX_CHARS, overlap=_SPLADE_CHUNK_OVERLAP)

        if len(chunks) == 1:
            embeddings = list(self._model.embed(chunks, batch_size=1))  # type: ignore[union-attr]
            if not embeddings:
                return SparseVector()
            emb = embeddings[0]
            return SparseVector(
                indices=emb.indices.tolist(),
                values=emb.values.tolist(),
            )

        # Encode all chunks and max-pool across vocab indices
        merged: dict[int, float] = {}
        embeddings = list(self._model.embed(chunks, batch_size=len(chunks)))  # type: ignore[union-attr]
        for emb in embeddings:
            for idx, val in zip(emb.indices.tolist(), emb.values.tolist(), strict=True):
                if idx not in merged or val > merged[idx]:
                    merged[idx] = val

        if not merged:
            return SparseVector()

        sorted_items = sorted(merged.items())
        logger.debug(
            "SPLADE chunked %d chars into %d chunks, %d merged dims",
            len(text), len(chunks), len(sorted_items),
        )
        return SparseVector(
            indices=[i for i, _ in sorted_items],
            values=[v for _, v in sorted_items],
        )

    def index_memory(self, memory: Memory) -> None:
        """Encode a memory's content and store its sparse vector."""
        self._ensure_model()
        sv = self._embed_chunked(memory.content)
        if sv.indices:
            self._vectors[memory.id] = sv

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
