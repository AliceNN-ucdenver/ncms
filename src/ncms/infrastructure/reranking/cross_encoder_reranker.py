"""Cross-encoder reranking for candidate list refinement.

Uses a lightweight cross-encoder model (e.g., ms-marco-MiniLM-L-6-v2) to
compute query-document relevance scores. Sentence-transformers CrossEncoder
is already a project dependency — no new packages needed.

Typical latency: ~30-80ms for 50 candidates on Apple Silicon (MPS).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)

# Global lock to prevent concurrent model loads (transformers monkey-patches
# nn.Module.register_parameter during from_pretrained — not thread-safe)
_model_load_lock = threading.Lock()


class CrossEncoderReranker:
    """Lazy-loaded cross-encoder for candidate reranking."""

    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: CrossEncoder | None = None
        self._load_failed: bool = False

    def _ensure_model(self) -> None:
        if self._model is None and not self._load_failed:
            with _model_load_lock:
                if self._model is not None:
                    logger.debug("[CrossEncoder] Model already loaded, skipping init")
                    return  # Another thread loaded while we waited
                try:
                    import time as _time

                    from sentence_transformers import CrossEncoder

                    kwargs: dict = {}
                    if self._cache_dir:
                        kwargs["cache_folder"] = self._cache_dir
                    logger.info("[CrossEncoder] Loading model: %s", self._model_name)
                    t0 = _time.perf_counter()
                    self._model = CrossEncoder(self._model_name, **kwargs)
                    load_ms = (_time.perf_counter() - t0) * 1000
                    logger.info("[CrossEncoder] Model loaded (%.0fms)", load_ms)
                except Exception as e:
                    logger.warning(
                        "[CrossEncoder] Load failed (reranking disabled): %s", e,
                    )
                    self._load_failed = True
        elif self._model is not None:
            logger.debug("[CrossEncoder] Model already loaded, skipping init")

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
        if self._model is None:
            # Model failed to load — return candidates with zero scores
            return [(mid, 0.0) for mid, _ in candidates[:top_k]]

        if not candidates:
            return []

        # Truncate content to avoid exceeding model's token limit (512 tokens ~ 2000 chars)
        pairs = [(query, content[:2000]) for _, content in candidates]
        scores = self._model.predict(pairs, batch_size=32, show_progress_bar=False)

        scored = list(zip(
            [mid for mid, _ in candidates],
            scores.tolist(),
            strict=True,
        ))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
