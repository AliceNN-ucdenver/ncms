"""Unit tests for the SPLADE sparse retrieval engine.

Mocks fastembed to avoid downloading the ~530 MB model during CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from ncms.domain.models import Memory
from ncms.infrastructure.indexing.splade_engine import SparseVector, SpladeEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockSparseEmbedding:
    """Mimics fastembed's SparseEmbedding namedtuple."""

    def __init__(self, indices: list[int], values: list[float]):
        self.indices = np.array(indices)
        self.values = np.array(values)


def _deterministic_embed(texts: list[str], batch_size: int = 1):
    """Return deterministic sparse vectors based on word hashing."""
    results = []
    for text in texts:
        words = text.lower().split()[:10]
        indices = [abs(hash(w)) % 1000 for w in words]
        values = [1.0] * len(indices)
        results.append(MockSparseEmbedding(indices, values))
    return results


def _make_memory(content: str, memory_id: str | None = None) -> Memory:
    mem = Memory(content=content, type="fact")
    if memory_id:
        mem.id = memory_id
    return mem


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSpladeEngine:
    @patch("ncms.infrastructure.indexing.splade_engine.SpladeEngine._ensure_model")
    def test_index_and_search_returns_results(self, mock_ensure):
        """Indexed memories should be retrievable via search."""
        engine = SpladeEngine()
        engine._model = MagicMock()
        engine._model.embed = _deterministic_embed

        mem1 = _make_memory("Flask web framework for REST APIs", "mem-1")
        mem2 = _make_memory("Django template rendering system", "mem-2")
        mem3 = _make_memory("Flask REST API endpoint design", "mem-3")

        engine.index_memory(mem1)
        engine.index_memory(mem2)
        engine.index_memory(mem3)

        assert engine.vector_count == 3

        results = engine.search("Flask REST API")
        assert len(results) > 0
        # All results should have positive scores
        for mid, score in results:
            assert score > 0.0

    @patch("ncms.infrastructure.indexing.splade_engine.SpladeEngine._ensure_model")
    def test_remove_excludes_from_search(self, mock_ensure):
        """Removed memory should not appear in search results."""
        engine = SpladeEngine()
        engine._model = MagicMock()
        engine._model.embed = _deterministic_embed

        mem1 = _make_memory("Flask API", "mem-1")
        mem2 = _make_memory("Flask routing", "mem-2")

        engine.index_memory(mem1)
        engine.index_memory(mem2)
        assert engine.vector_count == 2

        engine.remove("mem-1")
        assert engine.vector_count == 1

        results = engine.search("Flask")
        result_ids = {mid for mid, _ in results}
        assert "mem-1" not in result_ids

    @patch("ncms.infrastructure.indexing.splade_engine.SpladeEngine._ensure_model")
    def test_search_empty_index_returns_empty(self, mock_ensure):
        """Searching an empty index should return an empty list."""
        engine = SpladeEngine()
        engine._model = MagicMock()
        engine._model.embed = _deterministic_embed

        results = engine.search("anything")
        assert results == []

    @patch("ncms.infrastructure.indexing.splade_engine.SpladeEngine._ensure_model")
    def test_vector_count_tracks_indexed(self, mock_ensure):
        """vector_count should reflect the number of indexed memories."""
        engine = SpladeEngine()
        engine._model = MagicMock()
        engine._model.embed = _deterministic_embed

        assert engine.vector_count == 0
        engine.index_memory(_make_memory("first", "m-1"))
        assert engine.vector_count == 1
        engine.index_memory(_make_memory("second", "m-2"))
        assert engine.vector_count == 2
        engine.remove("m-1")
        assert engine.vector_count == 1

    @patch("ncms.infrastructure.indexing.splade_engine.SpladeEngine._ensure_model")
    def test_scores_positive_and_sorted_descending(self, mock_ensure):
        """All returned scores should be positive and sorted descending."""
        engine = SpladeEngine()
        engine._model = MagicMock()
        engine._model.embed = _deterministic_embed

        for i in range(5):
            engine.index_memory(_make_memory(f"document number {i} about APIs", f"m-{i}"))

        results = engine.search("APIs document")
        for i in range(len(results)):
            assert results[i][1] > 0.0
            if i > 0:
                assert results[i][1] <= results[i - 1][1]

    def test_import_error_without_fastembed(self):
        """When fastembed is not installed, _ensure_model should raise ImportError."""
        engine = SpladeEngine()
        with patch.dict("sys.modules", {"fastembed": None}):
            with pytest.raises(ImportError, match="fastembed is required"):
                engine._ensure_model()

    @patch("ncms.infrastructure.indexing.splade_engine.SpladeEngine._ensure_model")
    def test_remove_nonexistent_is_safe(self, mock_ensure):
        """Removing a non-existent memory should not raise."""
        engine = SpladeEngine()
        engine.remove("does-not-exist")  # Should not raise
