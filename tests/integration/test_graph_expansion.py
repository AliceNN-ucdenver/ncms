"""Integration tests for Tier 1.5 graph-expanded retrieval.

Tests that graph expansion discovers memories via shared entities
that BM25 missed lexically, and handles all edge cases correctly.
"""

from __future__ import annotations

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.domain.models import AccessRecord
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


async def _create_expansion_service(**overrides) -> tuple[MemoryService, SQLiteStore]:
    """Create a MemoryService for graph expansion tests.

    Returns (service, store) — caller should ``await store.close()`` when done.
    """
    defaults: dict = dict(
        db_path=":memory:",
        actr_noise=0.0,
        graph_expansion_depth=1,
        graph_expansion_max=10,
    )
    defaults.update(overrides)
    config = NCMSConfig(**defaults)
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    return MemoryService(store=store, index=index, graph=graph, config=config), store


class TestGraphExpansion:
    """Tests for Tier 1.5 graph-expanded candidate discovery."""

    @pytest.mark.asyncio
    async def test_finds_related_memory_via_shared_entity(self):
        """Graph expansion should discover memories via shared entities that BM25 misses."""
        svc, store = await _create_expansion_service()
        try:
            # Memory A: mentions PostgreSQL with "connection pooling" vocabulary
            mem_a = await svc.store_memory(
                content="PostgreSQL connection pooling configuration via PgBouncer",
                domains=["db"],
            )
            # Memory B: mentions PostgreSQL with completely different vocabulary
            mem_b = await svc.store_memory(
                content="PostgreSQL replication setup for read replicas",
                domains=["db"],
            )

            # Query matches "connection pooling" lexically (BM25 hits mem_a)
            # Graph expansion should find mem_b via shared "PostgreSQL" entity
            results = await svc.search("connection pooling configuration")
            result_ids = [r.memory.id for r in results]

            assert mem_a.id in result_ids, "BM25 hit should be in results"
            assert mem_b.id in result_ids, "Graph-expanded memory should be in results"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_expansion_always_on(self):
        """Graph expansion is always on — shared entities connect documents."""
        svc, store = await _create_expansion_service()
        try:
            mem_a = await svc.store_memory(
                content="PostgreSQL connection pooling configuration via PgBouncer",
                domains=["db"],
            )
            mem_b = await svc.store_memory(
                content="PostgreSQL replication setup for read replicas",
                domains=["db"],
            )

            results = await svc.search("connection pooling configuration")
            result_ids = [r.memory.id for r in results]

            # mem_a should be found by BM25
            assert mem_a.id in result_ids
            # mem_b should be discovered via shared PostgreSQL entity
            assert mem_b.id in result_ids
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_expansion_respects_depth_limit(self):
        """depth=1 should find 1-hop neighbors; depth=2 should find 2-hop."""
        svc, store = await _create_expansion_service(graph_expansion_depth=1)
        try:
            # Create two memories with entities linked through a relationship chain:
            # PostgreSQL -> (used_by) -> FastAPI -> (used_by) -> React
            # mem_a links to PostgreSQL, mem_c links to React (2 hops apart)

            # Memory A: has PostgreSQL entity
            mem_a = await svc.store_memory(
                content="PostgreSQL database stores user profile data",
                domains=["db"],
            )
            # Memory B: has both PostgreSQL and FastAPI entities (bridge)
            mem_b = await svc.store_memory(
                content="FastAPI backend connects to PostgreSQL for data access",
                domains=["api"],
            )
            # Memory C: has FastAPI and React entities (2 hops from PostgreSQL-only)
            await svc.store_memory(
                content="React frontend calls the FastAPI REST endpoints",
                domains=["frontend"],
            )

            # Add explicit relationships to create the chain
            pg_eid = svc.graph.find_entity_by_name("PostgreSQL")
            fastapi_eid = svc.graph.find_entity_by_name("FastAPI")
            react_eid = svc.graph.find_entity_by_name("React")

            if pg_eid and fastapi_eid:
                await svc.add_relationship(pg_eid, fastapi_eid, "used_by")
            if fastapi_eid and react_eid:
                await svc.add_relationship(fastapi_eid, react_eid, "used_by")

            # Query that BM25-matches only mem_a (PostgreSQL data storage)
            results_d1 = await svc.search("PostgreSQL data storage")
            result_ids_d1 = [r.memory.id for r in results_d1]

            # With depth=1: should find mem_a (BM25) and mem_b (1-hop via PostgreSQL)
            assert mem_a.id in result_ids_d1
            # mem_b shares PostgreSQL entity, should be discovered at depth=1
            assert mem_b.id in result_ids_d1
        finally:
            await store.close()

        # Now test with depth=2
        svc2, store2 = await _create_expansion_service(graph_expansion_depth=2)
        try:
            mem_a2 = await svc2.store_memory(
                content="PostgreSQL database stores user profile data",
                domains=["db"],
            )
            mem_b2 = await svc2.store_memory(
                content="FastAPI backend connects to PostgreSQL for data access",
                domains=["api"],
            )
            mem_c2 = await svc2.store_memory(
                content="React frontend calls the FastAPI REST endpoints",
                domains=["frontend"],
            )

            pg_eid2 = svc2.graph.find_entity_by_name("PostgreSQL")
            fastapi_eid2 = svc2.graph.find_entity_by_name("FastAPI")
            react_eid2 = svc2.graph.find_entity_by_name("React")

            if pg_eid2 and fastapi_eid2:
                await svc2.add_relationship(pg_eid2, fastapi_eid2, "used_by")
            if fastapi_eid2 and react_eid2:
                await svc2.add_relationship(fastapi_eid2, react_eid2, "used_by")

            results_d2 = await svc2.search("PostgreSQL data storage")
            result_ids_d2 = [r.memory.id for r in results_d2]

            # With depth=2: should find all three
            assert mem_a2.id in result_ids_d2
            assert mem_b2.id in result_ids_d2
            # mem_c2 is 2 hops away via PostgreSQL->FastAPI->React
            assert mem_c2.id in result_ids_d2
        finally:
            await store2.close()

    @pytest.mark.asyncio
    async def test_expansion_respects_max_limit(self):
        """Graph expansion should not exceed graph_expansion_max candidates."""
        max_expand = 3
        svc, store = await _create_expansion_service(graph_expansion_max=max_expand)
        try:
            # Store many memories sharing the "Redis" entity
            seed_content = "Redis cache invalidation strategy for session management"
            await svc.store_memory(content=seed_content, domains=["cache"])

            # Store additional memories that all share "Redis" but differ lexically
            for i in range(8):
                await svc.store_memory(
                    content=f"Redis cluster shard configuration number {i} for scaling",
                    domains=["cache"],
                )

            # Query that BM25 matches the seed (cache invalidation)
            results = await svc.search("cache invalidation strategy")

            # Count how many results have bm25_score == 0.0 (graph-only discoveries)
            graph_only = [r for r in results if r.bm25_score == 0.0]
            assert len(graph_only) <= max_expand
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_bm25_hits_not_duplicated(self):
        """A memory found by both BM25 and graph should appear exactly once."""
        svc, store = await _create_expansion_service()
        try:
            mem = await svc.store_memory(
                content="PostgreSQL connection pooling with PgBouncer",
                domains=["db"],
            )

            results = await svc.search("PostgreSQL connection pooling")
            result_ids = [r.memory.id for r in results]

            # Should appear exactly once, not duplicated by graph expansion
            assert result_ids.count(mem.id) == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_empty_graph_no_expansion(self):
        """Plain text with no extractable entities should not trigger expansion."""
        svc, store = await _create_expansion_service()
        try:
            mem = await svc.store_memory(
                content="the quick brown fox jumps over the lazy dog",
                domains=["misc"],
            )

            results = await svc.search("quick brown fox")

            # Should still find the memory via BM25
            if results:
                result_ids = [r.memory.id for r in results]
                assert mem.id in result_ids
            # No graph-only results since there are no entities
            graph_only = [r for r in results if r.bm25_score == 0.0]
            assert len(graph_only) == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_graph_discovered_has_zero_bm25_score(self):
        """Memories found only via graph expansion should have bm25_score == 0.0."""
        svc, store = await _create_expansion_service()
        try:
            # mem_a: BM25 matchable
            await svc.store_memory(
                content="PostgreSQL connection pooling configuration via PgBouncer",
                domains=["db"],
            )
            # mem_b: shares PostgreSQL entity but different vocabulary
            mem_b = await svc.store_memory(
                content="PostgreSQL replication setup for read replicas",
                domains=["db"],
            )

            results = await svc.search("connection pooling configuration")
            mem_b_results = [r for r in results if r.memory.id == mem_b.id]

            if mem_b_results:
                # mem_b was found via graph expansion (not BM25 lexical match)
                assert mem_b_results[0].bm25_score == 0.0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_graph_discovered_can_outrank_weak_bm25(self):
        """A frequently accessed graph memory can outrank a weak BM25 hit."""
        svc, store = await _create_expansion_service()
        try:
            # mem_weak: weak BM25 match (shares few terms with query)
            mem_weak = await svc.store_memory(
                content="configuration file setup instructions for the application",
                domains=["ops"],
            )
            # mem_graph: shares PostgreSQL entity, has been accessed many times
            mem_graph = await svc.store_memory(
                content="PostgreSQL tuning parameters for production workloads",
                domains=["db"],
            )
            # Also store a memory that links PostgreSQL to the query context
            await svc.store_memory(
                content="PostgreSQL configuration and connection settings",
                domains=["db"],
            )

            # Access mem_graph many times to boost its base-level activation
            for _ in range(10):
                await store.log_access(AccessRecord(memory_id=mem_graph.id, accessing_agent="test"))

            # Query: "PostgreSQL configuration" — BM25 matches the third memory
            # and may weakly match mem_weak on "configuration".
            # Graph expansion should discover mem_graph via shared PostgreSQL entity.
            results = await svc.search("PostgreSQL configuration")
            result_ids = [r.memory.id for r in results]

            # mem_graph should appear in results (via graph expansion)
            assert mem_graph.id in result_ids

            # If both appear, mem_graph should rank higher due to access frequency
            if mem_weak.id in result_ids:
                graph_idx = result_ids.index(mem_graph.id)
                weak_idx = result_ids.index(mem_weak.id)
                assert graph_idx <= weak_idx, (
                    "Frequently accessed graph memory should rank at or above weak BM25 hit"
                )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_domain_filter_applies_to_expanded(self):
        """Domain filter should apply to graph-expanded candidates too."""
        svc, store = await _create_expansion_service()
        try:
            # mem_api: in "api" domain, mentions PostgreSQL
            await svc.store_memory(
                content="PostgreSQL backed API endpoint for user management",
                domains=["api"],
            )
            # mem_db: in "db" domain, shares PostgreSQL entity
            mem_db = await svc.store_memory(
                content="PostgreSQL replication configuration for high availability",
                domains=["db"],
            )

            # Search with domain filter for "api" only
            results = await svc.search("PostgreSQL management", domain="api")
            result_ids = [r.memory.id for r in results]

            # mem_db should be excluded by domain filter even if graph-expanded
            assert mem_db.id not in result_ids
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_no_expansion_on_empty_bm25(self):
        """If BM25 returns nothing, there's nothing to expand from."""
        svc, store = await _create_expansion_service()
        try:
            await svc.store_memory(
                content="PostgreSQL database configuration",
                domains=["db"],
            )

            # Query that matches absolutely nothing
            results = await svc.search("xyzzy foobar nonexistent gibberish")
            assert len(results) == 0
        finally:
            await store.close()
