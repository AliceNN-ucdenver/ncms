"""Integration test: superseded state ranks lower than current state in search results."""

from __future__ import annotations

import pytest
import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.application.reconciliation_service import ReconciliationService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    AccessRecord,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def index() -> TantivyEngine:
    engine = TantivyEngine()
    engine.initialize()
    return engine


@pytest.fixture
def graph() -> NetworkXGraph:
    return NetworkXGraph()


@pytest.fixture
def config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,  # Deterministic for testing
        reconciliation_enabled=True,
        reconciliation_supersession_penalty=0.3,
        reconciliation_conflict_penalty=0.15,
    )


@pytest_asyncio.fixture
async def recon_service(store, config):
    return ReconciliationService(store=store, config=config)


@pytest_asyncio.fixture
async def memory_service(store, index, graph, config, recon_service):
    return MemoryService(
        store=store, index=index, graph=graph, config=config,
        reconciliation=recon_service,
    )


async def _create_memory_with_entity_state(
    store: SQLiteStore,
    index: TantivyEngine,
    content: str,
    entity_id: str,
    state_key: str,
    state_value: str,
    *,
    domains: list[str] | None = None,
) -> tuple[Memory, MemoryNode]:
    """Create a Memory + index it + create an entity state MemoryNode.

    Also logs an initial access so ACT-R base-level activation doesn't
    filter the memory out of search results (retrieval_probability > 0.05).
    """
    mem = Memory(
        content=content,
        domains=domains or ["test"],
    )
    await store.save_memory(mem)
    index.index_memory(mem)
    # Log initial access so ACT-R base level is computable
    await store.log_access(AccessRecord(memory_id=mem.id))

    node = MemoryNode(
        memory_id=mem.id,
        node_type=NodeType.ENTITY_STATE,
        importance=5.0,
        metadata={
            "entity_id": entity_id,
            "state_key": state_key,
            "state_value": state_value,
        },
    )
    await store.save_memory_node(node)
    return mem, node


class TestSupersededStateRanksLower:
    """Superseded memories should rank lower than current memories in search."""

    async def test_current_state_ranks_higher_than_superseded(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        graph: NetworkXGraph,
        config: NCMSConfig,
        memory_service: MemoryService,
        recon_service: ReconciliationService,
    ) -> None:
        """When searching, a superseded memory should score lower due to penalty."""
        # Create v1 (will be superseded)
        mem_v1, node_v1 = await _create_memory_with_entity_state(
            store, index,
            "auth-service status is running in production",
            "auth-service", "status", "running",
        )

        # Create v2 (supersedes v1)
        mem_v2, node_v2 = await _create_memory_with_entity_state(
            store, index,
            "auth-service status is stopped for maintenance",
            "auth-service", "status", "stopped",
        )
        await recon_service.reconcile(node_v2)

        # Verify v1 is now superseded
        v1_updated = await store.get_memory_node(node_v1.id)
        assert v1_updated is not None
        assert v1_updated.is_current is False

        # Search for auth-service status
        results = await memory_service.search("auth-service status", limit=10)
        assert len(results) >= 2

        # Find the v1 and v2 results
        v1_result = next((r for r in results if r.memory.id == mem_v1.id), None)
        v2_result = next((r for r in results if r.memory.id == mem_v2.id), None)

        assert v1_result is not None
        assert v2_result is not None

        # v2 (current) should have higher activation than v1 (superseded)
        assert v2_result.total_activation > v1_result.total_activation

        # v1 should be annotated as superseded
        assert v1_result.is_superseded is True
        assert v2_result.is_superseded is False

    async def test_scored_memory_annotations_populated(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        graph: NetworkXGraph,
        config: NCMSConfig,
        memory_service: MemoryService,
        recon_service: ReconciliationService,
    ) -> None:
        """ScoredMemory annotations reflect reconciliation state."""
        mem, node = await _create_memory_with_entity_state(
            store, index,
            "database version is 3.2 on production cluster",
            "db-main", "version", "3.2",
        )

        # Not superseded — annotations should be clean
        results = await memory_service.search("database version", limit=10)
        result = next((r for r in results if r.memory.id == mem.id), None)
        assert result is not None
        assert result.is_superseded is False
        assert result.has_conflicts is False
        assert result.superseded_by is None

    async def test_conflict_annotation_in_search(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        graph: NetworkXGraph,
        config: NCMSConfig,
        memory_service: MemoryService,
        recon_service: ReconciliationService,
    ) -> None:
        """Memories with CONFLICTS_WITH edges should be annotated."""
        mem_us, node_us = await _create_memory_with_entity_state(
            store, index,
            "api-gateway status is running in us-east-1 region",
            "api-gw", "status", "running",
        )
        node_us.metadata["state_scope"] = "us-east-1"
        await store.update_memory_node(node_us)

        mem_eu, node_eu = await _create_memory_with_entity_state(
            store, index,
            "api-gateway status is degraded in eu-west-1 region",
            "api-gw", "status", "degraded",
        )
        node_eu.metadata["state_scope"] = "eu-west-1"
        await store.update_memory_node(node_eu)
        await recon_service.reconcile(node_eu)

        # Search — both should appear, but with conflict annotations
        results = await memory_service.search("api-gateway status", limit=10)
        eu_result = next((r for r in results if r.memory.id == mem_eu.id), None)
        assert eu_result is not None
        assert eu_result.has_conflicts is True
