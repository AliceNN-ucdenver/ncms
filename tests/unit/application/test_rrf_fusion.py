"""Unit tests for Reciprocal Rank Fusion in MemoryService."""

from __future__ import annotations

from ncms.application.memory_service import MemoryService


class TestRRFFusion:
    def test_rrf_disjoint_lists(self):
        """Disjoint BM25 and SPLADE results should produce union."""
        bm25 = [("a", 10.0), ("b", 8.0)]
        splade = [("c", 5.0), ("d", 3.0)]

        fused = MemoryService._rrf_fuse(bm25, splade)

        result_ids = [mid for mid, _ in fused]
        assert set(result_ids) == {"a", "b", "c", "d"}
        assert len(fused) == 4

    def test_rrf_overlapping_boosts_shared(self):
        """Items appearing in both lists should rank higher than single-source."""
        bm25 = [("shared", 10.0), ("bm25_only", 8.0)]
        splade = [("shared", 5.0), ("splade_only", 3.0)]

        fused = MemoryService._rrf_fuse(bm25, splade)

        # "shared" should be first since it appears in both lists
        assert fused[0][0] == "shared"
        # Its score should be higher than any single-source item
        shared_score = fused[0][1]
        other_scores = [score for mid, score in fused if mid != "shared"]
        assert all(shared_score > s for s in other_scores)

    def test_rrf_empty_splade_preserves_bm25(self):
        """When SPLADE returns nothing, order should follow BM25."""
        bm25 = [("a", 10.0), ("b", 5.0), ("c", 1.0)]
        splade: list[tuple[str, float]] = []

        fused = MemoryService._rrf_fuse(bm25, splade)

        result_ids = [mid for mid, _ in fused]
        assert result_ids == ["a", "b", "c"]

    def test_rrf_empty_bm25_preserves_splade(self):
        """When BM25 returns nothing, order should follow SPLADE."""
        bm25: list[tuple[str, float]] = []
        splade = [("x", 8.0), ("y", 3.0)]

        fused = MemoryService._rrf_fuse(bm25, splade)

        result_ids = [mid for mid, _ in fused]
        assert result_ids == ["x", "y"]

    def test_rrf_both_empty(self):
        """Both empty should return empty."""
        fused = MemoryService._rrf_fuse([], [])
        assert fused == []

    def test_rrf_scores_are_positive(self):
        """All RRF scores should be positive floats."""
        bm25 = [("a", 10.0), ("b", 5.0)]
        splade = [("c", 3.0)]

        fused = MemoryService._rrf_fuse(bm25, splade)

        for mid, score in fused:
            assert score > 0.0

    def test_rrf_sorted_descending(self):
        """Results should be sorted by RRF score descending."""
        bm25 = [("a", 10.0), ("b", 8.0), ("c", 6.0)]
        splade = [("d", 5.0), ("b", 4.0), ("e", 2.0)]

        fused = MemoryService._rrf_fuse(bm25, splade)

        scores = [score for _, score in fused]
        assert scores == sorted(scores, reverse=True)
