"""Integration tests for observed_at + reference_time wiring (P1 temporal).

Verifies the full path:
- ``store_memory(observed_at=X)`` persists X on the Memory and propagates
  to the L1 node's ``observed_at``.
- ``search(..., reference_time=Y)`` passes Y to ``parse_temporal_reference``
  so temporal expressions resolve relative to Y.
- Temporal scoring actually differentiates memories with different
  ``observed_at`` values when the query has a temporal expression.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.domain.models import NodeType
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def svc() -> MemoryService:
    """Fresh in-memory MemoryService with temporal scoring enabled."""
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        temporal_enabled=True,
        scoring_weight_temporal=0.5,
        scoring_weight_bm25=0.6,
        scoring_weight_splade=0.0,
        scoring_weight_graph=0.0,
    )
    service = MemoryService(
        store=store, index=index, graph=graph, config=config,
    )
    yield service
    await store.close()


class TestObservedAtPersistence:
    """observed_at survives the store -> load round-trip."""

    async def test_store_memory_persists_observed_at(
        self, svc: MemoryService,
    ) -> None:
        past = datetime(2023, 4, 10, 12, 0, tzinfo=UTC)
        memory = await svc.store_memory(
            content="historical fact",
            observed_at=past,
        )
        reloaded = await svc._store.get_memory(memory.id)
        assert reloaded is not None
        assert reloaded.observed_at == past

    async def test_observed_at_defaults_to_none(
        self, svc: MemoryService,
    ) -> None:
        memory = await svc.store_memory(content="no observed date")
        reloaded = await svc._store.get_memory(memory.id)
        assert reloaded is not None
        assert reloaded.observed_at is None


class TestL1NodePropagation:
    """The L1 atomic node carries observed_at from the Memory."""

    async def test_l1_node_gets_observed_at(
        self, svc: MemoryService,
    ) -> None:
        past = datetime(2023, 4, 10, 12, 0, tzinfo=UTC)
        memory = await svc.store_memory(
            content="event at known time",
            observed_at=past,
        )
        # Drain background indexing to make sure the L1 node is written
        await svc.flush_indexing()
        nodes = await svc._store.get_memory_nodes_for_memory(memory.id)
        atomic_nodes = [n for n in nodes if n.node_type == NodeType.ATOMIC]
        assert atomic_nodes, "L1 atomic node must exist"
        assert atomic_nodes[0].observed_at == past


class TestSearchReferenceTime:
    """search(reference_time=...) changes how the parser resolves dates."""

    async def test_reference_time_shifts_temporal_range(
        self, svc: MemoryService,
    ) -> None:
        """'yesterday' with reference_time in 2023 ranks the
        near-date memory above the far-date one."""
        ref_time = datetime(2023, 4, 11, 12, 0, tzinfo=UTC)
        match_date = datetime(2023, 4, 10, 12, 0, tzinfo=UTC)   # day before ref
        distant_date = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)

        # Near-duplicate content (distinguishable by one word so dedup
        # doesn't collapse them) with different observed_at. BM25
        # surfaces both; temporal scoring must break the tie.
        await svc.store_memory(
            content="morning meeting with Alice discussing milestones apples",
            observed_at=match_date,
        )
        await svc.store_memory(
            content="morning meeting with Alice discussing milestones bananas",
            observed_at=distant_date,
        )
        await svc.flush_indexing()

        results = await svc.search(
            query="what did we discuss yesterday morning",
            limit=2,
            reference_time=ref_time,
        )
        assert len(results) == 2
        # The memory close to reference_time should rank first
        assert results[0].memory.observed_at == match_date


class TestTemporalScoringNonZero:
    """Sanity check: with wiring in place, temporal_score actually fires."""

    async def test_temporal_score_populated_for_temporal_query(
        self, svc: MemoryService,
    ) -> None:
        ref_time = datetime(2023, 4, 11, 12, 0, tzinfo=UTC)
        obs = datetime(2023, 4, 10, 12, 0, tzinfo=UTC)
        # Content must share tokens with the query so BM25 surfaces it.
        await svc.store_memory(
            content="project status update happened yesterday morning",
            observed_at=obs,
        )
        await svc.flush_indexing()
        results = await svc.search(
            query="what happened yesterday",
            limit=1,
            reference_time=ref_time,
        )
        assert results, "expected at least one result"
        # temporal_score is the weighted contribution; must be > 0 for a
        # memory whose observed_at is inside the parsed temporal range.
        assert results[0].temporal_score > 0.0

    async def test_no_temporal_expression_leaves_score_zero(
        self, svc: MemoryService,
    ) -> None:
        obs = datetime(2023, 4, 10, 12, 0, tzinfo=UTC)
        await svc.store_memory(
            content="beta analysis report",
            observed_at=obs,
        )
        await svc.flush_indexing()
        # Query has no temporal expression → parser returns None
        results = await svc.search(
            query="beta analysis",
            limit=1,
            reference_time=datetime(2023, 4, 11, tzinfo=UTC),
        )
        assert results
        assert results[0].temporal_score == 0.0


class TestBackwardCompatibility:
    """Existing callers that don't pass observed_at or reference_time work."""

    async def test_store_without_observed_at(
        self, svc: MemoryService,
    ) -> None:
        memory = await svc.store_memory(content="plain memory")
        assert memory.observed_at is None

    async def test_search_without_reference_time(
        self, svc: MemoryService,
    ) -> None:
        await svc.store_memory(content="plain memory")
        await svc.flush_indexing()
        results = await svc.search(query="plain memory", limit=1)
        # Should just work with default now=wall-clock
        assert results

    @pytest.mark.parametrize("raw", [
        "2023/04/10 (Mon) 23:07",
        "2023/04/10",
        "2023-04-10 12:00",
        "2023-04-10",
    ])
    def test_harness_date_parser_handles_known_formats(
        self, raw: str,
    ) -> None:
        from benchmarks.longmemeval.harness import _parse_lme_date
        dt = _parse_lme_date(raw)
        assert dt is not None
        assert dt.year == 2023
        assert dt.month == 4
        assert dt.day == 10

    def test_harness_date_parser_returns_none_on_junk(self) -> None:
        from benchmarks.longmemeval.harness import _parse_lme_date
        assert _parse_lme_date("") is None
        assert _parse_lme_date(None) is None
        assert _parse_lme_date("not a date") is None
