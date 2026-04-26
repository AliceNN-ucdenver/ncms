"""Integration tests: intent-aware retrieval pipeline end-to-end."""

from __future__ import annotations

import pytest
import pytest_asyncio

from ncms.application.memory_service import MemoryService
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
def intent_config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        temporal_enabled=True,
        scoring_weight_hierarchy=0.10,
        intent_hierarchy_bonus=0.5,
    )


@pytest.fixture
def no_intent_config() -> NCMSConfig:
    return NCMSConfig(db_path=":memory:", actr_noise=0.0)


@pytest_asyncio.fixture
async def intent_svc(store, index, graph, intent_config):
    return MemoryService(store=store, index=index, graph=graph, config=intent_config)


@pytest_asyncio.fixture
async def plain_svc(store, index, graph, no_intent_config):
    return MemoryService(store=store, index=index, graph=graph, config=no_intent_config)


async def _store_and_index(
    store: SQLiteStore,
    index: TantivyEngine,
    content: str,
    *,
    domains: list[str] | None = None,
    node_type: NodeType = NodeType.ATOMIC,
) -> tuple[Memory, MemoryNode]:
    """Store a memory, index it, create a typed node, and log access."""
    mem = Memory(content=content, domains=domains or ["test"])
    await store.save_memory(mem)
    index.index_memory(mem)
    await store.log_access(AccessRecord(memory_id=mem.id))

    node = MemoryNode(memory_id=mem.id, node_type=node_type, importance=5.0)
    await store.save_memory_node(node)
    return mem, node


class TestIntentAwareRetrieval:
    """Full pipeline tests: intent classification → type-filtered scoring."""

    async def test_intent_field_populated_when_enabled(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        intent_svc: MemoryService,
    ) -> None:
        """With intent enabled, results carry the classified intent."""
        await _store_and_index(store, index, "The auth service uses JWT tokens")

        results = await intent_svc.search("How does auth work?")
        assert len(results) >= 1
        # fact_lookup is the default for generic queries
        assert results[0].intent == "fact_lookup"

    async def test_intent_field_none_when_disabled(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        plain_svc: MemoryService,
    ) -> None:
        """With intent disabled, intent field is None."""
        await _store_and_index(store, index, "The auth service uses JWT tokens")

        results = await plain_svc.search("How does auth work?")
        assert len(results) >= 1
        assert results[0].intent is None

    async def test_node_types_populated(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        intent_svc: MemoryService,
    ) -> None:
        """Results include the node_types of the matched memory."""
        await _store_and_index(
            store,
            index,
            "Database schema version 5",
            node_type=NodeType.ENTITY_STATE,
        )

        results = await intent_svc.search("What is the database schema?")
        assert len(results) >= 1
        assert "entity_state" in results[0].node_types

    async def test_hierarchy_bonus_boosts_matching_type(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        intent_svc: MemoryService,
    ) -> None:
        """Entity state results get hierarchy bonus for state lookup queries."""
        # Store both an atomic and entity_state about the same topic
        mem_atomic, _ = await _store_and_index(
            store,
            index,
            "The API version is currently v2",
            node_type=NodeType.ATOMIC,
        )
        mem_state, _ = await _store_and_index(
            store,
            index,
            "The API version currently set to v2",
            node_type=NodeType.ENTITY_STATE,
        )

        # "What is" triggers current_state_lookup → entity_state gets bonus
        results = await intent_svc.search("What is the current API version?")
        assert len(results) >= 2

        # Find both results
        state_result = next(
            (r for r in results if r.memory.id == mem_state.id),
            None,
        )
        atomic_result = next(
            (r for r in results if r.memory.id == mem_atomic.id),
            None,
        )
        assert state_result is not None
        assert atomic_result is not None

        # Entity state should have hierarchy bonus > 0
        assert state_result.hierarchy_bonus > 0
        # Atomic should have no bonus for current_state_lookup
        assert atomic_result.hierarchy_bonus == 0

    async def test_event_reconstruction_boosts_episode(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        intent_svc: MemoryService,
    ) -> None:
        """Episode nodes get hierarchy bonus for event reconstruction queries."""
        # Atomic memory
        await _store_and_index(
            store,
            index,
            "The deployment incident caused downtime",
            node_type=NodeType.ATOMIC,
        )
        # Episode-typed memory
        _, ep_node = await _store_and_index(
            store,
            index,
            "The deployment incident timeline and resolution",
            node_type=NodeType.EPISODE,
        )

        # "what happened" + "incident" → event_reconstruction
        results = await intent_svc.search("What happened during the deployment incident?")
        assert len(results) >= 1

        ep_result = next(
            (r for r in results if "episode" in r.node_types),
            None,
        )
        if ep_result:
            assert ep_result.hierarchy_bonus > 0

    async def test_disabled_config_unchanged_pipeline(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        plain_svc: MemoryService,
    ) -> None:
        """Disabled intent doesn't affect search results."""
        await _store_and_index(store, index, "Users table schema definition")

        results = await plain_svc.search("users table")
        assert len(results) >= 1
        assert results[0].intent is None
        assert results[0].hierarchy_bonus == 0.0
        assert results[0].node_types == []  # No node preload when disabled

    async def test_low_confidence_falls_back(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        intent_svc: MemoryService,
    ) -> None:
        """Ambiguous query with weak signal falls back to fact_lookup."""
        await _store_and_index(store, index, "API endpoint documentation")

        # "endpoint" doesn't match any intent patterns strongly
        results = await intent_svc.search("endpoint documentation")
        assert len(results) >= 1
        assert results[0].intent == "fact_lookup"

    async def test_change_detection_intent_classified(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        intent_svc: MemoryService,
    ) -> None:
        """Change detection queries are properly classified."""
        await _store_and_index(
            store,
            index,
            "Schema changed from v4 to v5",
            node_type=NodeType.ENTITY_STATE,
        )

        results = await intent_svc.search("What changed in the schema?")
        assert len(results) >= 1
        assert results[0].intent == "change_detection"

    async def test_multiple_results_same_intent(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        intent_svc: MemoryService,
    ) -> None:
        """All results from same query share the same intent classification."""
        await _store_and_index(store, index, "Auth service status is healthy")
        await _store_and_index(store, index, "Auth service current configuration")

        results = await intent_svc.search("What is the current auth service status?")
        if len(results) >= 2:
            assert results[0].intent == results[1].intent
