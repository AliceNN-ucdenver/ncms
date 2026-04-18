"""Unit tests for ``ScoringPipeline.apply_ordinal_rerank``.

P1b: when the query carries ordinal intent ("first" / "last" /
"latest" / etc.), reorder the top-K candidates by event time so the
answer's chronological position wins regardless of lexical match
strength.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ncms.application.scoring.pipeline import ScoringPipeline
from ncms.domain.models import Memory, ScoredMemory


def _sm(id_: str, when: datetime, activation: float) -> ScoredMemory:
    """Build a ScoredMemory with a given observed_at + activation."""
    mem = Memory(content=f"memory {id_}", observed_at=when)
    mem.id = id_  # deterministic ids for assertions
    return ScoredMemory(memory=mem, total_activation=activation)


@pytest.fixture
def sample_pool() -> list[ScoredMemory]:
    """Five candidates, sorted by relevance (activation) descending.

    Observed times are deliberately *not* monotonic with activation so
    the rerank has something real to do.
    """
    return [
        _sm("newest",  datetime(2026, 4, 10, tzinfo=UTC), 0.90),
        _sm("middle",  datetime(2024, 6, 15, tzinfo=UTC), 0.85),
        _sm("oldest",  datetime(2022, 1, 20, tzinfo=UTC), 0.80),
        _sm("recent",  datetime(2026, 1, 30, tzinfo=UTC), 0.70),
        _sm("older",   datetime(2023, 3, 11, tzinfo=UTC), 0.60),
    ]


class TestApplyOrdinalRerank:
    """P1b contract: rerank top-K by observed_at when ordinal intent."""

    def test_no_temporal_ref_is_noop(self, sample_pool) -> None:
        out = ScoringPipeline.apply_ordinal_rerank(sample_pool, None)
        assert [s.memory.id for s in out] == [
            "newest", "middle", "oldest", "recent", "older",
        ]

    def test_temporal_ref_without_ordinal_is_noop(
        self, sample_pool,
    ) -> None:
        # A range-filter temporal ref (no ordinal) must not touch order.
        tr = SimpleNamespace(
            range_start=datetime(2024, 1, 1, tzinfo=UTC),
            range_end=datetime(2024, 12, 31, tzinfo=UTC),
            recency_bias=False,
            ordinal=None,
        )
        out = ScoringPipeline.apply_ordinal_rerank(sample_pool, tr)
        assert [s.memory.id for s in out] == [
            "newest", "middle", "oldest", "recent", "older",
        ]

    def test_empty_input_is_noop(self) -> None:
        tr = SimpleNamespace(ordinal="first")
        assert ScoringPipeline.apply_ordinal_rerank([], tr) == []

    def test_ordinal_first_sorts_ascending_by_observed_at(
        self, sample_pool,
    ) -> None:
        tr = SimpleNamespace(ordinal="first")
        out = ScoringPipeline.apply_ordinal_rerank(sample_pool, tr)
        # Ordered by observed_at ascending across the full pool (k >= 5)
        assert [s.memory.id for s in out] == [
            "oldest", "older", "middle", "recent", "newest",
        ]

    def test_ordinal_last_sorts_descending_by_observed_at(
        self, sample_pool,
    ) -> None:
        tr = SimpleNamespace(ordinal="last")
        out = ScoringPipeline.apply_ordinal_rerank(sample_pool, tr)
        assert [s.memory.id for s in out] == [
            "newest", "recent", "middle", "older", "oldest",
        ]

    def test_rerank_k_bounds_the_head(self) -> None:
        """Only the top-K are reordered; tail preserves relevance order."""
        pool = [
            _sm("a", datetime(2026, 1, 1, tzinfo=UTC), 1.0),
            _sm("b", datetime(2025, 1, 1, tzinfo=UTC), 0.9),
            _sm("c", datetime(2024, 1, 1, tzinfo=UTC), 0.8),
            _sm("d", datetime(2023, 1, 1, tzinfo=UTC), 0.7),
            _sm("e", datetime(2022, 1, 1, tzinfo=UTC), 0.6),
        ]
        tr = SimpleNamespace(ordinal="first")
        out = ScoringPipeline.apply_ordinal_rerank(
            pool, tr, rerank_k=3,
        )
        # Top-3 reordered (oldest first), tail (d, e) preserved
        assert [s.memory.id for s in out] == ["c", "b", "a", "d", "e"]

    def test_rerank_preserves_input_list(self, sample_pool) -> None:
        """The function must not mutate its input."""
        before = [s.memory.id for s in sample_pool]
        tr = SimpleNamespace(ordinal="first")
        ScoringPipeline.apply_ordinal_rerank(sample_pool, tr)
        after = [s.memory.id for s in sample_pool]
        assert before == after

    def test_missing_observed_at_falls_back_to_created_at(self) -> None:
        """Candidates without observed_at still sort — via created_at."""
        m1 = Memory(content="no_obs")
        m1.id = "m1"
        # Force a known created_at in the past
        m1.created_at = datetime(2020, 1, 1, tzinfo=UTC)

        m2 = Memory(
            content="with_obs",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        m2.id = "m2"

        pool = [
            ScoredMemory(memory=m1, total_activation=1.0),
            ScoredMemory(memory=m2, total_activation=0.5),
        ]
        tr = SimpleNamespace(ordinal="first")
        out = ScoringPipeline.apply_ordinal_rerank(pool, tr)
        # m1 is older (via created_at fallback), should come first
        assert [s.memory.id for s in out] == ["m1", "m2"]

    def test_unknown_ordinal_value_is_noop(self, sample_pool) -> None:
        """Defensive: an ordinal we don't recognize doesn't scramble."""
        tr = SimpleNamespace(ordinal="middle")  # not a valid value
        out = ScoringPipeline.apply_ordinal_rerank(sample_pool, tr)
        assert [s.memory.id for s in out] == [
            s.memory.id for s in sample_pool
        ]
