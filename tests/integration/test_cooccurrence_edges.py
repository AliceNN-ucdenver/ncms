"""Integration tests for co-occurrence edge creation during store_memory().

Validates that entities co-occurring within the same document automatically
create bidirectional edges in the in-memory entity graph, enabling graph
expansion to discover related memories.
"""

from __future__ import annotations

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


async def _create_service(**overrides) -> tuple[MemoryService, SQLiteStore, NetworkXGraph]:
    defaults: dict = dict(
        db_path=":memory:",
        actr_noise=0.0,
    )
    defaults.update(overrides)
    config = NCMSConfig(**defaults)
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    return MemoryService(store=store, index=index, graph=graph, config=config), store, graph


class TestCooccurrenceEdges:

    @pytest.mark.asyncio
    async def test_creates_edges_between_cooccurring_entities(self):
        """Storing a memory with N entities should create N*(N-1) directed edges."""
        svc, store, graph = await _create_service()
        try:
            await svc.store_memory(
                content="PostgreSQL connection pooling via PgBouncer for FastAPI backend",
                domains=["db"],
            )
            edge_count = graph._graph.number_of_edges()
            entity_count = graph._graph.number_of_nodes()

            # Should have edges (at least some entities co-occurring)
            assert edge_count > 0, "Co-occurrence should create graph edges"
            assert entity_count > 1, "GLiNER should extract multiple entities"
            # Bidirectional: N*(N-1) edges for N entities
            assert edge_count <= entity_count * (entity_count - 1)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_shared_entities_connect_documents(self):
        """Two documents sharing an entity should be reachable via graph traversal."""
        svc, store, graph = await _create_service()
        try:
            mem_a = await svc.store_memory(
                content="PostgreSQL connection pooling configuration via PgBouncer",
                domains=["db"],
            )
            mem_b = await svc.store_memory(
                content="PostgreSQL replication setup for read replicas",
                domains=["db"],
            )

            # Get entity IDs for mem_a
            mem_a_entities = graph.get_entity_ids_for_memory(mem_a.id)

            # Graph expansion from mem_a's entities should reach mem_b
            related = graph.get_related_memory_ids(mem_a_entities, depth=1)
            assert mem_b.id in related, (
                "Graph expansion should discover mem_b via shared PostgreSQL entity"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_always_creates_edges(self):
        """Co-occurrence is always on — edges created for any multi-entity memory."""
        svc, store, graph = await _create_service()
        try:
            await svc.store_memory(
                content="PostgreSQL connection pooling via PgBouncer for FastAPI backend",
                domains=["db"],
            )
            assert graph._graph.number_of_edges() > 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_entity_cap_limits_pairs(self):
        """Documents with many entities should be capped to cooccurrence_max_entities."""
        svc, store, graph = await _create_service(cooccurrence_max_entities=3)
        try:
            # Content designed to produce many entities
            await svc.store_memory(
                content=(
                    "PostgreSQL database with Redis cache and FastAPI backend "
                    "using SQLAlchemy ORM and Alembic migrations for Django "
                    "with Celery task queue and RabbitMQ message broker"
                ),
                domains=["infra"],
            )
            # With cap=3, max edges = 3*2 = 6 (3 choose 2 = 3 pairs × 2 directions)
            assert graph._graph.number_of_edges() <= 6
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_single_entity_no_edges(self):
        """A document with only one entity should create no co-occurrence edges."""
        svc, store, graph = await _create_service()
        try:
            await svc.store_memory(
                content="simple text about PostgreSQL",
                domains=["db"],
            )
            # May or may not extract entities, but if only 1, no edges
            if graph._graph.number_of_nodes() <= 1:
                assert graph._graph.number_of_edges() == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_graph_expansion_search_with_cooccurrence(self):
        """Full search pipeline should discover documents via co-occurrence edges."""
        svc, store, graph = await _create_service(
            graph_expansion_depth=1,
        )
        try:
            # Doc A: about PostgreSQL + connection pooling
            mem_a = await svc.store_memory(
                content="PostgreSQL connection pooling configuration via PgBouncer",
                domains=["db"],
            )
            # Doc B: about PostgreSQL + replication (no lexical overlap with "pooling")
            mem_b = await svc.store_memory(
                content="PostgreSQL replication setup for read replicas",
                domains=["db"],
            )

            results = await svc.search("connection pooling configuration")
            result_ids = [r.memory.id for r in results]

            assert mem_a.id in result_ids, "BM25 should find mem_a directly"
            # mem_b should be discoverable via co-occurrence edges
            # (PostgreSQL entity in mem_a → co-occurs with other entities → shared with mem_b)
            assert mem_b.id in result_ids, (
                "Graph expansion via co-occurrence edges should discover mem_b"
            )
        finally:
            await store.close()
