"""Unit tests for SQLiteStore.get_memory_nodes_for_memories() batch query."""

from __future__ import annotations

import pytest
import pytest_asyncio

from ncms.domain.models import Memory, MemoryNode, NodeType
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


class TestGetMemoryNodesForMemories:
    """Tests for batch memory node loading."""

    async def test_empty_input_returns_empty(self, store: SQLiteStore) -> None:
        result = await store.get_memory_nodes_for_memories([])
        assert result == {}

    async def test_returns_nodes_grouped_by_memory_id(
        self, store: SQLiteStore,
    ) -> None:
        """Two memories with one node each → dict with two entries."""
        mem1 = Memory(content="first memory", domains=["test"])
        mem2 = Memory(content="second memory", domains=["test"])
        await store.save_memory(mem1)
        await store.save_memory(mem2)

        node1 = MemoryNode(memory_id=mem1.id, node_type=NodeType.ATOMIC, importance=5.0)
        node2 = MemoryNode(memory_id=mem2.id, node_type=NodeType.ENTITY_STATE, importance=5.0)
        await store.save_memory_node(node1)
        await store.save_memory_node(node2)

        result = await store.get_memory_nodes_for_memories([mem1.id, mem2.id])

        assert len(result) == 2
        assert len(result[mem1.id]) == 1
        assert result[mem1.id][0].node_type == NodeType.ATOMIC
        assert len(result[mem2.id]) == 1
        assert result[mem2.id][0].node_type == NodeType.ENTITY_STATE

    async def test_missing_memory_ids_omitted(self, store: SQLiteStore) -> None:
        """Memory IDs with no nodes are absent from the result dict."""
        result = await store.get_memory_nodes_for_memories(["nonexistent-id"])
        assert result == {}

    async def test_multiple_nodes_per_memory_id(self, store: SQLiteStore) -> None:
        """One memory with two nodes → list of 2."""
        mem = Memory(content="multi-node memory", domains=["test"])
        await store.save_memory(mem)

        node1 = MemoryNode(memory_id=mem.id, node_type=NodeType.ATOMIC, importance=5.0)
        node2 = MemoryNode(
            memory_id=mem.id, node_type=NodeType.ENTITY_STATE, importance=5.0,
        )
        await store.save_memory_node(node1)
        await store.save_memory_node(node2)

        result = await store.get_memory_nodes_for_memories([mem.id])

        assert len(result) == 1
        assert len(result[mem.id]) == 2
        node_types = {n.node_type for n in result[mem.id]}
        assert NodeType.ATOMIC in node_types
        assert NodeType.ENTITY_STATE in node_types

    async def test_mixed_existing_and_missing(self, store: SQLiteStore) -> None:
        """Batch with some existing and some missing IDs."""
        mem = Memory(content="existing memory", domains=["test"])
        await store.save_memory(mem)
        node = MemoryNode(memory_id=mem.id, node_type=NodeType.ATOMIC, importance=5.0)
        await store.save_memory_node(node)

        result = await store.get_memory_nodes_for_memories(
            [mem.id, "missing-id-1", "missing-id-2"],
        )

        assert len(result) == 1
        assert mem.id in result
