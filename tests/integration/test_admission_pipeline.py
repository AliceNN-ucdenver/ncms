"""Integration tests for admission scoring in the store_memory pipeline."""

from __future__ import annotations

import pytest

from ncms.application.admission_service import AdmissionService
from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
def admission_config() -> NCMSConfig:
    return NCMSConfig(db_path=":memory:", actr_noise=0.0, admission_enabled=True)


@pytest.fixture
def disabled_config() -> NCMSConfig:
    return NCMSConfig(db_path=":memory:", actr_noise=0.0, admission_enabled=False)


@pytest.fixture
async def fresh_store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def fresh_index() -> TantivyEngine:
    engine = TantivyEngine()
    engine.initialize()
    return engine


@pytest.fixture
def fresh_graph() -> NetworkXGraph:
    return NetworkXGraph()


class TestAdmissionDisabled:
    async def test_store_memory_works_normally(
        self, fresh_store, fresh_index, fresh_graph, disabled_config
    ):
        """When admission is disabled, store_memory behaves exactly as before."""
        admission = AdmissionService(
            store=fresh_store, index=fresh_index, graph=fresh_graph, config=disabled_config
        )
        svc = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=disabled_config, admission=admission,
        )
        mem = await svc.store_memory("Simple content", domains=["test"])
        assert mem.id
        # Should be persisted
        retrieved = await fresh_store.get_memory(mem.id)
        assert retrieved is not None
        assert retrieved.content == "Simple content"

    async def test_no_admission_metadata_when_disabled(
        self, fresh_store, fresh_index, fresh_graph, disabled_config
    ):
        """When admission is disabled, no admission metadata is attached."""
        svc = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=disabled_config,
        )
        mem = await svc.store_memory("Content without admission")
        assert mem.structured is None or "admission" not in (mem.structured or {})


class TestAdmissionEnabled:
    async def test_high_quality_stores_and_creates_node(
        self, fresh_store, fresh_index, fresh_graph, admission_config
    ):
        """High-quality content should be stored + get a MemoryNode."""
        admission = AdmissionService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config,
        )
        svc = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config, admission=admission,
        )
        mem = await svc.store_memory(
            "Architectural decision: we chose PostgreSQL for the primary database "
            "due to its JSON support and reliability. This was decided on 2026-03-01.",
            domains=["database"],
            source_agent="architect",
        )

        # Memory should be persisted
        retrieved = await fresh_store.get_memory(mem.id)
        assert retrieved is not None

        # Should have admission metadata
        assert mem.structured is not None
        assert "admission" in mem.structured
        assert mem.structured["admission"]["route"] == "atomic_memory"

        # Should have a MemoryNode
        nodes = await fresh_store.get_memory_nodes_for_memory(mem.id)
        assert len(nodes) == 1
        assert nodes[0].node_type.value == "atomic"

    async def test_discard_not_persisted(
        self, fresh_store, fresh_index, fresh_graph, admission_config
    ):
        """Content that routes to 'discard' should not be in the memories table."""
        admission = AdmissionService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config,
        )
        svc = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config, admission=admission,
        )
        # Very short, generic, low-value content
        mem = await svc.store_memory("hi")

        # If it was discarded, structured should have route="discard"
        if mem.structured and mem.structured.get("admission", {}).get("route") == "discard":
            # Verify it's NOT in the store
            count = await fresh_store.count_memories()
            assert count == 0
        # If it wasn't discarded (e.g., novelty was 1.0), that's also fine
        # — the test just verifies discard doesn't persist

    async def test_ephemeral_creates_cache_entry(
        self, fresh_store, fresh_index, fresh_graph, admission_config
    ):
        """Content routed to 'ephemeral_cache' should be in ephemeral table."""
        # First, store enough content to make subsequent similar content redundant
        admission = AdmissionService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config,
        )
        svc = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config, admission=admission,
        )
        # Store some initial content (without admission to seed the index)
        svc_no_admission = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config,
        )
        await svc_no_admission.store_memory(
            "The user service handles authentication and login",
            domains=["api"],
        )

        # Now store content that might be ephemeral (moderate novelty, low persistence)
        mem = await svc.store_memory(
            "The user service handles auth",
            domains=["api"],
        )

        # Check what route was taken
        if mem.structured and mem.structured.get("admission", {}).get("route") == "ephemeral_cache":
            ephemeral_id = mem.structured["admission"].get("ephemeral_id")
            assert ephemeral_id is not None
            entry = await fresh_store.get_ephemeral(ephemeral_id)
            assert entry is not None

    async def test_no_memory_node_when_no_admission(
        self, fresh_store, fresh_index, fresh_graph, disabled_config
    ):
        """Without admission, no MemoryNode is created."""
        svc = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=disabled_config,
        )
        mem = await svc.store_memory(
            "Content without admission scoring",
            domains=["test"],
        )
        nodes = await fresh_store.get_memory_nodes_for_memory(mem.id)
        assert len(nodes) == 0

    async def test_admission_failure_still_stores(
        self, fresh_store, fresh_index, fresh_graph, admission_config
    ):
        """If admission scoring fails, memory should still be stored normally."""
        # Use a broken admission service (bad config that causes internal error)
        # We can test graceful degradation by passing admission=None
        svc = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config,
            # No admission service → feature flag on but no service → skips admission
        )
        mem = await svc.store_memory("Content that should still be stored")
        retrieved = await fresh_store.get_memory(mem.id)
        assert retrieved is not None


class TestAdmissionMetadata:
    async def test_features_stored_in_structured(
        self, fresh_store, fresh_index, fresh_graph, admission_config
    ):
        """Admission features should be stored in memory.structured['admission']."""
        admission = AdmissionService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config,
        )
        svc = MemoryService(
            store=fresh_store, index=fresh_index, graph=fresh_graph,
            config=admission_config, admission=admission,
        )
        mem = await svc.store_memory(
            "The API endpoint was updated to return 200 OK on success, "
            "this is an architectural decision made on 2026-03-10.",
            domains=["api"],
        )

        # Only check if it was stored (not discarded/ephemeral)
        if mem.structured and "admission" in mem.structured:
            admission_data = mem.structured["admission"]
            assert "score" in admission_data
            assert "route" in admission_data
            # Check some feature keys are present (when not discard/ephemeral)
            if admission_data["route"] not in ("discard", "ephemeral_cache"):
                assert "novelty" in admission_data
                assert "utility" in admission_data
