"""Unit tests for ``RetrievalPipeline.apply_range_filter``.

Phase B.4 primitive — hard-filter candidates whose persisted
``memory_content_ranges`` row overlaps the query range.

Coverage matrix:

* Overlap semantics: inside / spans / crosses-start / crosses-end /
  equal / adjacent-but-disjoint.
* Missing-range policy: ``include`` (default, recall-safe) vs
  ``exclude`` (precision-safe).
* Degrade rules: empty candidates, empty/missing ranges, store error.
* Order preservation.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from ncms.application.retrieval.pipeline import RetrievalPipeline
from ncms.config import NCMSConfig
from ncms.domain.temporal_normalizer import NormalizedInterval, RawSpan


def _make_pipeline(ranges_map: dict[str, tuple[str, str]]):
    """Build a RetrievalPipeline with a stub store that returns ``ranges_map``
    from ``get_content_ranges_batch``."""
    store = MagicMock()
    store.get_content_ranges_batch = AsyncMock(return_value=ranges_map)
    return RetrievalPipeline(
        store=store,
        index=MagicMock(),
        graph=MagicMock(),
        config=NCMSConfig(db_path=":memory:"),
        splade=None,
        reranker=None,
    )


def _iv(start: str, end: str) -> NormalizedInterval:
    """Build a NormalizedInterval from two ISO-8601 dates."""
    return NormalizedInterval(
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        confidence=0.9,
        source_span=RawSpan("test", "date"),
        origin="test",
    )


# Query range: 2024-06-01 → 2024-07-01 (all of June 2024).
QUERY_RANGE = _iv("2024-06-01T00:00:00+00:00", "2024-07-01T00:00:00+00:00")


class TestOverlapSemantics:

    async def test_memory_inside_query_range_passes(self) -> None:
        pipe = _make_pipeline({
            "inside": (
                "2024-06-15T00:00:00+00:00",
                "2024-06-16T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("inside", 0.9)], QUERY_RANGE,
        )
        assert out == [("inside", 0.9)]

    async def test_memory_spans_query_range_passes(self) -> None:
        """Memory range fully contains the query range → overlap."""
        pipe = _make_pipeline({
            "spans": (
                "2024-01-01T00:00:00+00:00",
                "2025-01-01T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("spans", 0.9)], QUERY_RANGE,
        )
        assert out == [("spans", 0.9)]

    async def test_memory_crosses_query_start_passes(self) -> None:
        """Memory May–June 15 → overlaps late May crossing into June."""
        pipe = _make_pipeline({
            "crosses_start": (
                "2024-05-15T00:00:00+00:00",
                "2024-06-15T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("crosses_start", 0.9)], QUERY_RANGE,
        )
        assert out == [("crosses_start", 0.9)]

    async def test_memory_crosses_query_end_passes(self) -> None:
        """Memory June 15 – July 15 → overlaps crossing end."""
        pipe = _make_pipeline({
            "crosses_end": (
                "2024-06-15T00:00:00+00:00",
                "2024-07-15T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("crosses_end", 0.9)], QUERY_RANGE,
        )
        assert out == [("crosses_end", 0.9)]

    async def test_memory_before_query_range_filtered(self) -> None:
        pipe = _make_pipeline({
            "before": (
                "2024-05-01T00:00:00+00:00",
                "2024-05-30T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("before", 0.9)], QUERY_RANGE,
        )
        assert out == []

    async def test_memory_after_query_range_filtered(self) -> None:
        pipe = _make_pipeline({
            "after": (
                "2024-07-15T00:00:00+00:00",
                "2024-07-20T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("after", 0.9)], QUERY_RANGE,
        )
        assert out == []

    async def test_adjacent_before_half_open_filtered(self) -> None:
        """Memory [May 1, June 1) is adjacent but disjoint from [June 1, July 1).
        Half-open intervals → no overlap."""
        pipe = _make_pipeline({
            "adj_before": (
                "2024-05-01T00:00:00+00:00",
                "2024-06-01T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("adj_before", 0.9)], QUERY_RANGE,
        )
        assert out == []

    async def test_adjacent_after_half_open_filtered(self) -> None:
        """Memory [July 1, Aug 1) is adjacent to [June 1, July 1). No overlap."""
        pipe = _make_pipeline({
            "adj_after": (
                "2024-07-01T00:00:00+00:00",
                "2024-08-01T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("adj_after", 0.9)], QUERY_RANGE,
        )
        assert out == []


class TestMissingRangePolicy:

    async def test_include_policy_passes_missing(self) -> None:
        """Default 'include' — missing range → pass (recall-safe)."""
        pipe = _make_pipeline({})  # no ranges for any memory
        out = await pipe.apply_range_filter(
            [("m1", 0.9), ("m2", 0.8)], QUERY_RANGE,
            missing_range_policy="include",
        )
        assert out == [("m1", 0.9), ("m2", 0.8)]

    async def test_exclude_policy_drops_missing(self) -> None:
        """Opt-in 'exclude' — missing range → drop (precision-safe)."""
        pipe = _make_pipeline({})
        out = await pipe.apply_range_filter(
            [("m1", 0.9), ("m2", 0.8)], QUERY_RANGE,
            missing_range_policy="exclude",
        )
        assert out == []

    async def test_mixed_presence_include(self) -> None:
        pipe = _make_pipeline({
            "has_range_in": (
                "2024-06-15T00:00:00+00:00",
                "2024-06-16T00:00:00+00:00",
            ),
            "has_range_out": (
                "2024-03-01T00:00:00+00:00",
                "2024-03-02T00:00:00+00:00",
            ),
        })
        out = await pipe.apply_range_filter(
            [("has_range_in", 0.9),
             ("has_range_out", 0.8),
             ("missing", 0.7)],
            QUERY_RANGE,
            missing_range_policy="include",
        )
        # has_range_in kept (overlap), has_range_out dropped (no overlap),
        # missing kept (include policy).
        assert out == [("has_range_in", 0.9), ("missing", 0.7)]


class TestDegrade:

    async def test_empty_candidates(self) -> None:
        pipe = _make_pipeline({})
        out = await pipe.apply_range_filter([], QUERY_RANGE)
        assert out == []

    async def test_store_error_returns_input_unchanged(self) -> None:
        """Database error on range lookup → filter is a no-op."""
        store = MagicMock()
        store.get_content_ranges_batch = AsyncMock(
            side_effect=RuntimeError("db gone"),
        )
        pipe = RetrievalPipeline(
            store=store, index=MagicMock(), graph=MagicMock(),
            config=NCMSConfig(db_path=":memory:"),
            splade=None, reranker=None,
        )
        out = await pipe.apply_range_filter(
            [("m1", 0.9), ("m2", 0.8)], QUERY_RANGE,
        )
        assert out == [("m1", 0.9), ("m2", 0.8)]


class TestOrderPreservation:

    async def test_order_preserved(self) -> None:
        """Surviving candidates keep their input order."""
        pipe = _make_pipeline({
            f"m{i}": (
                "2024-06-15T00:00:00+00:00",
                "2024-06-16T00:00:00+00:00",
            )
            for i in range(5)
        })
        input_candidates = [(f"m{i}", 1.0 - i * 0.1) for i in range(5)]
        out = await pipe.apply_range_filter(input_candidates, QUERY_RANGE)
        assert out == input_candidates  # all pass, order preserved
