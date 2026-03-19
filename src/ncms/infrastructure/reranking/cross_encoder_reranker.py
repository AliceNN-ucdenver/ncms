"""Cross-encoder reranking for candidate list refinement.

Uses a lightweight cross-encoder model (e.g., ms-marco-MiniLM-L-6-v2) to
compute query-document relevance scores. Sentence-transformers CrossEncoder
is already a project dependency — no new packages needed.

Typical latency: ~30-80ms for 50 candidates on Apple Silicon (MPS).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """Lazy-loaded cross-encoder for candidate reranking."""

    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: CrossEncoder | None = None

    def _ensure_model(self) -> None:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            kwargs: dict = {}
            if self._cache_dir:
                kwargs["cache_folder"] = self._cache_dir
            logger.info("Loading cross-encoder model: %s", self._model_name)
            self._model = CrossEncoder(self._model_name, **kwargs)
            logger.info("Cross-encoder model loaded")

    def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],  # (memory_id, content)
        top_k: int = 50,
    ) -> list[tuple[str, float]]:
        """Rerank candidates by cross-encoder relevance score.

        Args:
            query: The search query.
            candidates: List of (memory_id, content) tuples.
            top_k: Number of top results to return.

        Returns:
            List of (memory_id, ce_score) sorted by score descending.
        """
        self._ensure_model()
        assert self._model is not None  # noqa: S101

        if not candidates:
            return []

        # Truncate content to avoid exceeding model's token limit (512 tokens ~ 2000 chars)
        pairs = [(query, content[:2000]) for _, content in candidates]
        scores = self._model.predict(pairs, batch_size=32)

        scored = list(zip(
            [mid for mid, _ in candidates],
            scores.tolist(),
            strict=True,
        ))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
