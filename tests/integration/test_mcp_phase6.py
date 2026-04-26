"""Integration tests for Phase 6 MCP tool functionality."""

from __future__ import annotations

import pytest
import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.domain.models import Memory, MemoryNode, NodeType
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def services():
    """Create real services for integration testing."""
    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        temporal_enabled=True,
    )
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    memory_svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=config,
    )
    yield memory_svc, store, config
    await store.close()


class TestIntentOverrideSearch:
    """Test search with intent_override parameter."""

    async def test_valid_intent_override(self, services) -> None:
        memory_svc, store, config = services

        await memory_svc.store_memory(
            content="The auth service uses OAuth 2.0 protocol",
            memory_type="fact",
            domains=["api"],
        )
        results = await memory_svc.search(
            "auth protocol",
            intent_override="current_state_lookup",
        )
        assert len(results) >= 1
        assert results[0].intent == "current_state_lookup"

    async def test_invalid_intent_raises(self, services) -> None:
        memory_svc, store, config = services

        with pytest.raises(ValueError, match="Invalid intent"):
            await memory_svc.search("test", intent_override="nonexistent_intent")

    async def test_override_bypasses_classifier(self, services) -> None:
        memory_svc, store, config = services

        await memory_svc.store_memory(
            content="Pattern: all endpoints use REST conventions",
            memory_type="fact",
        )
        results = await memory_svc.search(
            "REST conventions",
            intent_override="pattern_lookup",
        )
        assert len(results) >= 1
        # Intent should match override, not auto-classified
        assert results[0].intent == "pattern_lookup"


class TestEntityStateQueries:
    """Test entity state tools via store methods."""

    async def test_get_current_state(self, services) -> None:
        memory_svc, store, config = services

        # Create backing memory + entity state node
        mem = Memory(id="mem-state-1", content="auth: OAuth 2.0", type="fact")
        await store.save_memory(mem)
        node = MemoryNode(
            memory_id="mem-state-1",
            node_type=NodeType.ENTITY_STATE,
            is_current=True,
            metadata={
                "entity_id": "auth-service",
                "state_key": "protocol",
                "state_value": "OAuth 2.0",
            },
        )
        await store.save_memory_node(node)

        result = await store.get_current_state("auth-service", "protocol")
        assert result is not None
        assert result.metadata["state_value"] == "OAuth 2.0"
        assert result.is_current is True

    async def test_get_state_history(self, services) -> None:
        memory_svc, store, config = services

        # Create two states
        for i, val in enumerate(["v1", "v2"]):
            mem = Memory(id=f"mem-h-{i}", content=f"version {val}", type="fact")
            await store.save_memory(mem)
            node = MemoryNode(
                memory_id=f"mem-h-{i}",
                node_type=NodeType.ENTITY_STATE,
                is_current=(i == 1),
                metadata={
                    "entity_id": "api-service",
                    "state_key": "version",
                    "state_value": val,
                },
            )
            await store.save_memory_node(node)

        history = await store.get_state_history("api-service", "version")
        assert len(history) == 2


class TestEpisodeQueries:
    """Test episode tools via store methods."""

    async def test_list_open_episodes(self, services) -> None:
        memory_svc, store, config = services

        mem = Memory(id="mem-ep-1", content="episode backing", type="fact")
        await store.save_memory(mem)
        ep = MemoryNode(
            memory_id="mem-ep-1",
            node_type=NodeType.EPISODE,
            metadata={"status": "open", "episode_title": "Deployment Incident"},
        )
        await store.save_memory_node(ep)

        open_eps = await store.get_open_episodes()
        assert len(open_eps) == 1
        assert open_eps[0].metadata["episode_title"] == "Deployment Incident"

    async def test_get_episode_members(self, services) -> None:
        memory_svc, store, config = services

        # Create episode
        mem = Memory(id="mem-ep-2", content="episode", type="fact")
        await store.save_memory(mem)
        ep = MemoryNode(
            memory_id="mem-ep-2",
            node_type=NodeType.EPISODE,
            metadata={"status": "open", "episode_title": "Test EP"},
        )
        await store.save_memory_node(ep)

        # Create member
        mem2 = Memory(id="mem-member-1", content="member content", type="fact")
        await store.save_memory(mem2)
        member = MemoryNode(
            memory_id="mem-member-1",
            node_type=NodeType.ATOMIC,
            parent_id=ep.id,
            metadata={"episode_id": ep.id},
        )
        await store.save_memory_node(member)

        members = await store.get_episode_members(ep.id)
        assert len(members) == 1
        assert members[0].memory_id == "mem-member-1"
