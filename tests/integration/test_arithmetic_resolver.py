"""Phase B.5 integration: ``MemoryService.compute_temporal_arithmetic``.

Deterministic, LLM-free answers to arithmetic temporal questions by
pulling ``observed_at`` metadata from graph-linked memories and doing
Python date math.

Seeded corpus: six life-event memories across 2024–2025 about MoMA,
the Metropolitan Museum, and Central Park.  Each memory's
``observed_at`` is the event date.

Scenarios:

1. ``between`` with two anchor entities → days delta, correct to the
   day.
2. ``between`` with two anchor entities asking for weeks → delta in
   weeks.
3. ``since`` with one anchor entity and a caller-supplied
   ``reference_time`` → delta against now.
4. ``age_of`` one anchor + reference_time → delta in the unit the
   query asks for.
5. Missing anchor (entity never mentioned) → returns ``None``.
6. Non-arithmetic query → returns ``None``.
7. Anchor memories present, ordinal/range/state queries → returns
   ``None`` (we're LLM-free and this resolver only fires on
   arithmetic intent).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

_CORPUS: list[tuple[str, datetime]] = [
    # MoMA events
    (
        "Visited MoMA with family — a highlight of the spring break.",
        datetime(2024, 4, 1, tzinfo=UTC),
    ),
    ("Returned to MoMA for the Vermeer retrospective.", datetime(2024, 11, 15, tzinfo=UTC)),
    # Metropolitan Museum events
    ("First trip to the Metropolitan Museum of Art in years.", datetime(2024, 6, 5, tzinfo=UTC)),
    (
        "Another Metropolitan Museum visit — the Egyptian wing again.",
        datetime(2025, 2, 20, tzinfo=UTC),
    ),
    # Central Park events
    ("Central Park autumn walk, beautiful foliage.", datetime(2024, 10, 10, tzinfo=UTC)),
    ("Central Park picnic for my aunt's birthday.", datetime(2025, 5, 3, tzinfo=UTC)),
]


@pytest_asyncio.fixture
async def svc() -> MemoryService:
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        splade_enabled=False,
        scoring_weight_splade=0.0,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.0,
        scoring_weight_graph=0.0,
        scoring_weight_recency=0.0,
        scoring_weight_temporal=0.0,
        temporal_enabled=True,
        temporal_range_filter_enabled=True,
    )
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=config,
    )
    for content, when in _CORPUS:
        await svc.store_memory(
            content=content,
            memory_type="fact",
            observed_at=when,
        )
    await svc.flush_indexing()
    yield svc
    await store.close()


class TestBetween:
    """Two-anchor arithmetic: ``between A and B``.

    Expected math:
      earliest MoMA = 2024-04-01
      earliest Met  = 2024-06-05
      delta = 65 days = ~9.3 weeks
    """

    async def test_between_days(self, svc: MemoryService) -> None:
        result = await svc.compute_temporal_arithmetic(
            query="How many days between my MoMA visit and the Metropolitan Museum trip?",
        )
        assert result is not None, "expected a resolver result"
        assert result.operation == "between"
        assert result.unit == "days"
        assert result.answer_value == 65.0, (
            f"65 days between 2024-04-01 and 2024-06-05, got {result.answer_value}"
        )
        assert result.answer_text == "65 days"
        assert len(result.anchor_memories) == 2
        assert len(result.anchor_dates) == 2
        # Chronological order.
        assert result.anchor_dates[0] < result.anchor_dates[1]

    async def test_between_weeks(self, svc: MemoryService) -> None:
        """Unqualified 'MoMA' + 'Metropolitan' — BM25 picks whichever
        memory scores best for the query text.  We assert the unit and
        operation but not the exact value because there's no
        qualifier to disambiguate which MoMA or which Met visit."""
        result = await svc.compute_temporal_arithmetic(
            query="How many weeks between MoMA and the Metropolitan?",
        )
        assert result is not None
        assert result.operation == "between"
        assert result.unit == "weeks"
        # Delta must be positive and the two anchors distinct.
        assert result.answer_value > 0.0
        assert len(result.anchor_memories) == 2
        assert result.anchor_memories[0].id != result.anchor_memories[1].id


class TestSince:
    """One-anchor arithmetic with a caller-supplied reference_time."""

    async def test_since_weeks_with_reference(
        self,
        svc: MemoryService,
    ) -> None:
        ref = datetime(2025, 5, 1, tzinfo=UTC)  # exactly 4 weeks after picnic
        result = await svc.compute_temporal_arithmetic(
            query="How many weeks since the MoMA retrospective?",
            reference_time=ref,
        )
        # MoMA retrospective = 2024-11-15; ref = 2025-05-01; delta = 167 days
        # 167 / 7 = 23.857... → 23.9
        assert result is not None
        assert result.operation == "since"
        assert result.unit == "weeks"
        assert abs(result.answer_value - 23.9) <= 0.1


class TestAgeOf:
    """'How long ago did X happen' — anchor + reference_time, most
    recent memory for the anchor."""

    async def test_how_long_ago_days(self, svc: MemoryService) -> None:
        """Qualified query — 'picnic' scopes to the May 2025 picnic
        memory via BM25 ranking, not the Oct 2024 walk.  This is
        the key behavioural test that query qualifiers route the
        resolver to the right anchor."""
        ref = datetime(2025, 6, 10, tzinfo=UTC)
        result = await svc.compute_temporal_arithmetic(
            query="How long ago did we have the Central Park picnic?",
            reference_time=ref,
        )
        # Picnic = 2025-05-03; ref = 2025-06-10; delta = 38 days.
        assert result is not None
        assert result.operation == "age_of"
        assert result.unit == "days"
        assert result.answer_value == 38.0
        # Anchor should be the picnic memory specifically.
        assert "picnic" in result.anchor_memories[0].content.lower()


class TestNoneCases:
    async def test_unknown_anchor_returns_none(
        self,
        svc: MemoryService,
    ) -> None:
        """Anchor entity never mentioned in any memory → None."""
        result = await svc.compute_temporal_arithmetic(
            query="How many days between Mars and Venus?",
        )
        assert result is None

    async def test_non_arithmetic_query_returns_none(
        self,
        svc: MemoryService,
    ) -> None:
        result = await svc.compute_temporal_arithmetic(
            query="Tell me about the Metropolitan Museum",
        )
        assert result is None

    async def test_ordinal_query_returns_none(
        self,
        svc: MemoryService,
    ) -> None:
        """Ordinal queries fall to the ordinal primitive, not arithmetic."""
        result = await svc.compute_temporal_arithmetic(
            query="What was the first MoMA visit?",
        )
        assert result is None

    async def test_range_query_returns_none(
        self,
        svc: MemoryService,
    ) -> None:
        result = await svc.compute_temporal_arithmetic(
            query="What did I do during 2024?",
        )
        assert result is None

    async def test_one_anchor_for_between_returns_none(
        self,
        svc: MemoryService,
    ) -> None:
        """'Between' needs 2 anchors; if only one resolves, return None."""
        result = await svc.compute_temporal_arithmetic(
            query="How many days between MoMA and Mars?",
        )
        assert result is None
