"""SPLADE sparse neural retrieval engine (splade-v3 via sentence-transformers).

Uses sentence-transformers' SparseEncoder for SPLADE v3 encoding with
automatic MPS/CUDA/CPU device selection.  Stores sparse vectors in-memory
for brute-force dot-product search.  Suitable for corpora up to ~100K memories.

Long texts are automatically chunked at sentence boundaries so each
chunk fits within SPLADE v3's 512-token window (~2,000 chars).  Sparse
vectors from each chunk are merged by taking the max weight per
vocabulary index, preserving the strongest signal across the document.

Key difference from v1: SPLADE v3 uses asymmetric encoding —
``encode_document()`` for indexing and ``encode_query()`` for search.

Disabled by default; enable via config.splade_enabled = True.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from ncms.domain.models import Memory
from ncms.infrastructure.text.chunking import chunk_text

logger = logging.getLogger(__name__)

# SPLADE v3's BERT backbone has a 512-token window.  At ~4 chars/token the safe
# character budget is ~2,000 chars, leaving headroom for special tokens.
_SPLADE_CHUNK_MAX_CHARS: int = 2000
_SPLADE_CHUNK_OVERLAP: int = 100


def _resolve_device() -> str:
    """Pick the best available device: MPS > CUDA > CPU.

    Override with ``NCMS_SPLADE_DEVICE=cpu|mps|cuda`` env var.
    """
    override = os.environ.get("NCMS_SPLADE_DEVICE", "").strip().lower()
    if override in ("cpu", "mps", "cuda"):
        return override

    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


@dataclass
class SparseVector:
    """A sparse vector: parallel arrays of vocabulary indices and weights."""

    indices: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)


def _tensor_to_sparse_vector(tensor) -> SparseVector:
    """Convert a PyTorch tensor (sparse or dense) to SparseVector."""
    import torch

    if isinstance(tensor, torch.Tensor):
        if tensor.is_sparse:
            tensor = tensor.to_dense()
        flat = tensor.flatten()
        nonzero_mask = flat != 0
        indices = torch.nonzero(nonzero_mask, as_tuple=True)[0]
        values = flat[indices]
        return SparseVector(
            indices=indices.tolist(),
            values=values.tolist(),
        )

    # scipy sparse fallback
    if hasattr(tensor, "toarray"):
        import numpy as np

        dense = np.asarray(tensor.toarray()).flatten()
        nonzero = np.nonzero(dense)[0]
        return SparseVector(
            indices=nonzero.tolist(),
            values=dense[nonzero].tolist(),
        )

    return SparseVector()


class SpladeEngine:
    """SPLADE sparse neural retrieval engine.

    Encodes text into sparse vectors via sentence-transformers' SparseEncoder
    using the SPLADE v3 model with MPS/CUDA/CPU auto-detection.
    Long texts are chunked and merged (max-pool per vocab index).
    Stores vectors in-memory (``dict[str, SparseVector]``) and performs
    brute-force dot-product search.  Suitable for corpora up to ~100K memories.

    SPLADE v3 uses asymmetric encoding: ``encode_document()`` for indexing
    and ``encode_query()`` for search queries.
    """

    def __init__(
        self,
        model_name: str = "naver/splade-v3",
        cache_dir: str | None = None,
    ):
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: object | None = None  # Lazy-loaded SparseEncoder
        self._vectors: dict[str, SparseVector] = {}
        self._lock = threading.Lock()  # Serializes model load + inference

    def _ensure_model(self) -> None:
        """Lazy-load the SPLADE model on first use."""
        with self._lock:
            if self._model is not None:
                logger.debug("[SPLADE] Model already loaded, skipping init")
                return
            import time as _time

            from sentence_transformers import SparseEncoder

            device = _resolve_device()
            logger.info(
                "[SPLADE] Loading model: %s on %s (first call only)", self._model_name, device,
            )
            kwargs: dict[str, object] = {}
            if self._cache_dir:
                kwargs["cache_folder"] = self._cache_dir
            t0 = _time.perf_counter()
            self._model = SparseEncoder(self._model_name, device=device, **kwargs)
            load_ms = (_time.perf_counter() - t0) * 1000
            logger.info("[SPLADE] Model loaded on %s (%.0fms)", device, load_ms)

    def _embed_document_chunked(self, text: str) -> SparseVector:
        """Encode document text with chunking, merging via max-pool per vocab index."""
        chunks = chunk_text(
            text, max_chars=_SPLADE_CHUNK_MAX_CHARS, overlap=_SPLADE_CHUNK_OVERLAP,
        )

        # Encode all chunks as documents (asymmetric: document side)
        # Lock serializes GPU access (PyTorch models are not thread-safe).
        with self._lock:
            embeddings = self._model.encode_document(  # type: ignore[union-attr]
                chunks, show_progress_bar=False,
            )

        if len(chunks) == 1:
            return _tensor_to_sparse_vector(embeddings[0])

        # Max-pool across vocab indices for multi-chunk documents
        merged: dict[int, float] = {}
        for emb in embeddings:
            sv = _tensor_to_sparse_vector(emb)
            for idx, val in zip(sv.indices, sv.values, strict=True):
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

    def _embed_query(self, query: str) -> SparseVector:
        """Encode a query string (asymmetric: query side)."""
        with self._lock:
            embeddings = self._model.encode_query(  # type: ignore[union-attr]
                [query], show_progress_bar=False,
            )
        if not len(embeddings):
            return SparseVector()
        return _tensor_to_sparse_vector(embeddings[0])

    def index_memory(self, memory: Memory) -> None:
        """Encode a memory's content and store its sparse vector."""
        self._ensure_model()
        sv = self._embed_document_chunked(memory.content)
        if sv.indices:
            self._vectors[memory.id] = sv

    def remove(self, memory_id: str) -> None:
        """Remove a memory's sparse vector from the store."""
        self._vectors.pop(memory_id, None)

    def search(self, query: str, limit: int = 50) -> list[tuple[str, float]]:
        """Search by SPLADE sparse dot-product similarity.

        Uses asymmetric encoding: query is encoded with ``encode_query()``
        (which produces different sparse representations than document encoding).

        Returns ``(memory_id, splade_score)`` pairs sorted descending by score.
        """
        if not self._vectors:
            return []

        self._ensure_model()
        q_sv = self._embed_query(query)
        if not q_sv.indices:
            return []

        q_map: dict[int, float] = dict(
            zip(q_sv.indices, q_sv.values, strict=True),
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

    def get_vector(self, memory_id: str) -> SparseVector | None:
        """Return the stored sparse vector for a memory, or None."""
        return self._vectors.get(memory_id)

    @staticmethod
    def cosine_similarity(a: SparseVector, b: SparseVector) -> float:
        """Cosine similarity between two sparse vectors.

        Returns 0.0 if either vector is empty or norms are zero.
        """
        if not a.indices or not b.indices:
            return 0.0

        # Build index → value map for the smaller vector (optimization)
        a_map = dict(zip(a.indices, a.values, strict=True))
        b_map = dict(zip(b.indices, b.values, strict=True))

        # Dot product — only overlapping indices contribute
        dot = 0.0
        for idx, val in a_map.items():
            if idx in b_map:
                dot += val * b_map[idx]

        if dot <= 0.0:
            return 0.0

        # L2 norms
        norm_a = sum(v * v for v in a.values) ** 0.5
        norm_b = sum(v * v for v in b.values) ** 0.5

        if norm_a <= 0.0 or norm_b <= 0.0:
            return 0.0

        return dot / (norm_a * norm_b)

    @staticmethod
    def max_pool_vectors(vectors: list[SparseVector]) -> SparseVector:
        """Max-pool multiple sparse vectors into a centroid.

        Takes the maximum weight per vocabulary index across all vectors.
        Same strategy as chunk merging in ``_embed_document_chunked()``.
        """
        if not vectors:
            return SparseVector()
        if len(vectors) == 1:
            return vectors[0]

        merged: dict[int, float] = {}
        for sv in vectors:
            for idx, val in zip(sv.indices, sv.values, strict=True):
                if idx not in merged or val > merged[idx]:
                    merged[idx] = val

        if not merged:
            return SparseVector()

        sorted_items = sorted(merged.items())
        return SparseVector(
            indices=[i for i, _ in sorted_items],
            values=[v for _, v in sorted_items],
        )

    @property
    def vector_count(self) -> int:
        """Number of stored sparse vectors."""
        return len(self._vectors)
