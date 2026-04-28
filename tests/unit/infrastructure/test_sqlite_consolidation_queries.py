"""Tests for SQLite consolidation query methods."""

from __future__ import annotations

import pytest_asyncio

from ncms.domain.models import Memory, MemoryNode, NodeType
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


async def _save_with_backing(
    store: SQLiteStore,
    node: MemoryNode,
) -> MemoryNode:
    """Create a backing Memory then save the node (satisfies FK constraint)."""
    mem = Memory(id=node.memory_id, content="backing", type="fact")
    await store.save_memory(mem)
    await store.save_memory_node(node)
    return node


class TestGetClosedUnsummarizedEpisodes:
    """Tests for get_closed_unsummarized_episodes."""

    async def test_returns_closed_unsummarized(self, store: SQLiteStore) -> None:
        node = MemoryNode(
            memory_id="mem-1",
            node_type=NodeType.EPISODE,
            metadata={"status": "closed", "episode_title": "Test"},
        )
        await _save_with_backing(store, node)

        result = await store.get_closed_unsummarized_episodes()
        assert len(result) == 1
        assert result[0].id == node.id

    async def test_excludes_open_episodes(self, store: SQLiteStore) -> None:
        await _save_with_backing(
            store,
            MemoryNode(
                memory_id="mem-1",
                node_type=NodeType.EPISODE,
                metadata={"status": "open", "episode_title": "Test"},
            ),
        )
        result = await store.get_closed_unsummarized_episodes()
        assert len(result) == 0

    async def test_excludes_already_summarized(self, store: SQLiteStore) -> None:
        await _save_with_backing(
            store,
            MemoryNode(
                memory_id="mem-1",
                node_type=NodeType.EPISODE,
                metadata={"status": "closed", "summarized": True},
            ),
        )
        result = await store.get_closed_unsummarized_episodes()
        assert len(result) == 0

    async def test_excludes_non_episode_nodes(self, store: SQLiteStore) -> None:
        await _save_with_backing(
            store,
            MemoryNode(
                memory_id="mem-1",
                node_type=NodeType.ATOMIC,
                metadata={"status": "closed"},
            ),
        )
        result = await store.get_closed_unsummarized_episodes()
        assert len(result) == 0


class TestGetEntitiesWithStateCount:
    """Tests for get_entities_with_state_count."""

    async def test_returns_entities_above_threshold(self, store: SQLiteStore) -> None:
        for i in range(4):
            await _save_with_backing(
                store,
                MemoryNode(
                    memory_id=f"mem-{i}",
                    node_type=NodeType.ENTITY_STATE,
                    metadata={"entity_id": "ent-1", "state_key": "version"},
                ),
            )
        result = await store.get_entities_with_state_count(3)
        assert len(result) == 1
        assert result[0] == ("ent-1", 4)

    async def test_excludes_entities_below_threshold(self, store: SQLiteStore) -> None:
        for i in range(2):
            await _save_with_backing(
                store,
                MemoryNode(
                    memory_id=f"mem-{i}",
                    node_type=NodeType.ENTITY_STATE,
                    metadata={"entity_id": "ent-1", "state_key": "version"},
                ),
            )
        result = await store.get_entities_with_state_count(3)
        assert len(result) == 0

    async def test_groups_by_entity_id(self, store: SQLiteStore) -> None:
        for i in range(3):
            await _save_with_backing(
                store,
                MemoryNode(
                    memory_id=f"mem-a-{i}",
                    node_type=NodeType.ENTITY_STATE,
                    metadata={"entity_id": "ent-a", "state_key": "k"},
                ),
            )
        for i in range(5):
            await _save_with_backing(
                store,
                MemoryNode(
                    memory_id=f"mem-b-{i}",
                    node_type=NodeType.ENTITY_STATE,
                    metadata={"entity_id": "ent-b", "state_key": "k"},
                ),
            )
        result = await store.get_entities_with_state_count(3)
        assert len(result) == 2
        assert result[0][0] == "ent-b"
        assert result[0][1] == 5


class TestGetAbstractNodesByType:
    """Tests for get_abstract_nodes_by_type."""

    async def test_filters_by_abstract_type(self, store: SQLiteStore) -> None:
        await _save_with_backing(
            store,
            MemoryNode(
                memory_id="mem-1",
                node_type=NodeType.ABSTRACT,
                metadata={"abstract_type": "episode_summary"},
            ),
        )
        await _save_with_backing(
            store,
            MemoryNode(
                memory_id="mem-2",
                node_type=NodeType.ABSTRACT,
                metadata={"abstract_type": "state_trajectory"},
            ),
        )
        result = await store.get_abstract_nodes_by_type("episode_summary")
        assert len(result) == 1
        assert result[0].metadata["abstract_type"] == "episode_summary"

    async def test_excludes_non_abstract_nodes(self, store: SQLiteStore) -> None:
        await _save_with_backing(
            store,
            MemoryNode(
                memory_id="mem-1",
                node_type=NodeType.ATOMIC,
                metadata={"abstract_type": "episode_summary"},
            ),
        )
        result = await store.get_abstract_nodes_by_type("episode_summary")
        assert len(result) == 0

    async def test_empty_when_no_matches(self, store: SQLiteStore) -> None:
        result = await store.get_abstract_nodes_by_type("nonexistent")
        assert result == []
