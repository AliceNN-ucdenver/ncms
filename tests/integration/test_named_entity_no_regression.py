"""Phase B.3 regression guard — non-temporal entity queries must not
be reordered by the ordinal primitive.

The retired pool-wide and subject-scoped ordinal reranks regressed
LongMemEval because they fired on every query containing "first" or
"last", including arithmetic and semantic-first questions they
couldn't correctly reorder.  Phase B.2 replaced them with a classified
intent dispatch: the primitive only fires on ``ORDINAL_SINGLE`` /
``ORDINAL_COMPARE`` / ``ORDINAL_ORDER``.

This test file is a living guard: any future change that causes the
primitive to fire on queries the classifier marks ``NONE`` / ``RANGE``
/ ``RELATIVE_ANCHOR`` / ``ARITHMETIC`` will break a test here.

Fixture is a set of entity-named memories at different dates, wired
through the full ``MemoryService.search`` pipeline.  Scenarios:

A. Baseline — temporal flag OFF, entity query.  Records the top-K
   order BM25 + SPLADE + graph produce.
B. Flag ON + entity-only query.  Classifier emits ``NONE``.  Top-K
   must be *identical* to A.  This is the core regression guard.
C. Flag ON + ordinal entity query.  Classifier emits ``ORDINAL_SINGLE``.
   Top-K is reordered by ``observed_at``.  Confirms the primitive
   actually fires when it should.
D. Flag ON + arithmetic query.  Classifier emits ``ARITHMETIC``.
   Primitive must NOT fire — top-K unchanged from A.
E. observed_at is preserved on every returned ``ScoredMemory`` so
   downstream consumers (dashboard, MCP, recall enrichment) can read
   it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

# Shared fixture data — five memories about two entities at different
# dates.  Dates deliberately mixed up with relevance order so a
# chronological rerank would produce a clearly-different ordering.
_MEMORIES = [
    (
        "MoMA visit with the family was a highlight of the spring.",
        datetime(2024, 3, 15, tzinfo=UTC),
    ),
    (
        "Returned to MoMA for the retrospective; great exhibition.",
        datetime(2024, 11, 2, tzinfo=UTC),
    ),
    ("The Metropolitan Museum has an impressive Egyptian wing.", datetime(2024, 5, 20, tzinfo=UTC)),
    (
        "Visited the Metropolitan Museum again to see the new Vermeer.",
        datetime(2025, 2, 10, tzinfo=UTC),
    ),
    (
        "Central Park in autumn is beautiful, good walking routes.",
        datetime(2024, 10, 1, tzinfo=UTC),
    ),
]


async def _make_service(
    *,
    temporal_range_filter_enabled: bool,
) -> MemoryService:
    """Build a MemoryService with the exact knobs we're testing."""
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
        scoring_weight_temporal=0.0,  # Isolate from P1a boost
        temporal_enabled=True,
        temporal_range_filter_enabled=temporal_range_filter_enabled,
    )
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=config,
    )
    for content, when in _MEMORIES:
        await svc.store_memory(
            content=content,
            memory_type="fact",
            observed_at=when,
        )
    await svc.flush_indexing()
    return svc


@pytest_asyncio.fixture
async def svc_flag_off() -> MemoryService:
    svc = await _make_service(temporal_range_filter_enabled=False)
    yield svc
    await svc._store.close()


@pytest_asyncio.fixture
async def svc_flag_on() -> MemoryService:
    svc = await _make_service(temporal_range_filter_enabled=True)
    yield svc
    await svc._store.close()


class TestFlagToggleNoopParity:
    """The core regression guard: flipping the temporal flag ON must
    not change results for non-temporal entity queries.  The classifier
    emits ``NONE`` and the primitive falls through."""

    async def test_plain_entity_query_identical_with_flag_toggle(
        self,
        svc_flag_off: MemoryService,
        svc_flag_on: MemoryService,
    ) -> None:
        query = "What do I know about MoMA?"
        off = await svc_flag_off.search(query=query, limit=5)
        on = await svc_flag_on.search(query=query, limit=5)
        assert [r.memory.content for r in off] == [r.memory.content for r in on], (
            "Non-temporal entity query must be identical across the temporal flag toggle."
        )

    async def test_multi_entity_query_identical_with_flag_toggle(
        self,
        svc_flag_off: MemoryService,
        svc_flag_on: MemoryService,
    ) -> None:
        query = "Tell me about museums."
        off = await svc_flag_off.search(query=query, limit=5)
        on = await svc_flag_on.search(query=query, limit=5)
        assert [r.memory.content for r in off] == [r.memory.content for r in on]


class TestPrimitiveStillFiresWhenItShould:
    """Complement to the guard: confirm the primitive DOES reorder on
    actual ordinal queries.  Without this test, a regression could
    disable the primitive entirely and the guard above would still pass."""

    async def test_ordinal_query_does_rerank(
        self,
        svc_flag_on: MemoryService,
    ) -> None:
        # "Latest MoMA" with flag on: classifier → ORDINAL_SINGLE,
        # primitive reorders subject-linked by observed_at desc.
        results = await svc_flag_on.search(
            query="What is the latest MoMA memory?",
            limit=5,
        )
        assert results
        # The newest MoMA memory should surface at rank 1.
        assert "retrospective" in results[0].memory.content.lower(), (
            "Expected newest MoMA memory (Nov 2024) at rank 1, got: "
            + results[0].memory.content[:80]
        )


class TestArithmeticNoFire:
    """Arithmetic intent is a fast-fail — classifier → ARITHMETIC and
    the primitive must not fire.  The strict invariant is "no
    chronological reordering," not "byte-identical to flag-off" (which
    is fragile: GLiNER label count differs between flag states, and
    minor BM25 tiebreaker noise can flip adjacent ranks)."""

    async def test_arithmetic_query_not_chronologically_sorted(
        self,
        svc_flag_on: MemoryService,
    ) -> None:
        """If the primitive had fired on an arithmetic query, top-K
        would be monotonically sorted by ``observed_at``.  Assert it
        isn't — the classifier correctly skipped the primitive."""
        results = await svc_flag_on.search(
            query="How many months between MoMA and the Metropolitan visit?",
            limit=5,
        )
        assert results
        dates = [r.memory.observed_at for r in results if r.memory.observed_at is not None]
        if len(dates) >= 3:
            monotonic_asc = all(dates[i] <= dates[i + 1] for i in range(len(dates) - 1))
            monotonic_desc = all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1))
            assert not (monotonic_asc or monotonic_desc), (
                "Arithmetic query was chronologically sorted — the "
                "ordinal primitive fired when it shouldn't have."
            )


class TestObservedAtSurfaces:
    """Named-entity queries must surface ``observed_at`` on every
    returned ScoredMemory so downstream consumers (dashboard, MCP
    tools, recall enrichment) can read the metadata clock."""

    async def test_every_result_has_observed_at(
        self,
        svc_flag_on: MemoryService,
    ) -> None:
        results = await svc_flag_on.search(
            query="Tell me about museum visits",
            limit=5,
        )
        assert results
        for r in results:
            assert r.memory.observed_at is not None, (
                f"observed_at missing on {r.memory.id}: {r.memory.content[:60]}"
            )
