"""Tests for SQLite memory graph persistence."""

from datetime import UTC, datetime, timedelta

import pytest

from ncms.domain.models import (
    EdgeType,
    EphemeralEntry,
    GraphEdge,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def memory(store: SQLiteStore) -> Memory:
    """A persisted Memory to reference from memory_nodes."""
    m = Memory(content="Test memory graph persistence", domains=["test"])
    await store.save_memory(m)
    return m


class TestV2Migration:
    async def test_memory_nodes_table_exists(self, store: SQLiteStore):
        cursor = await store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_nodes'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_graph_edges_table_exists(self, store: SQLiteStore):
        cursor = await store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_edges'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_ephemeral_cache_table_exists(self, store: SQLiteStore):
        cursor = await store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ephemeral_cache'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_existing_tables_still_exist(self, store: SQLiteStore):
        """V2 migration doesn't break V1 tables."""
        for table in ["memories", "entities", "relationships", "access_log", "snapshots"]:
            cursor = await store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            row = await cursor.fetchone()
            assert row is not None, f"V1 table '{table}' missing after V2 migration"


class TestMemoryNodeCRUD:
    async def test_save_and_get(self, store: SQLiteStore, memory: Memory):
        node = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ATOMIC,
            importance=7.0,
        )
        await store.save_memory_node(node)

        retrieved = await store.get_memory_node(node.id)
        assert retrieved is not None
        assert retrieved.memory_id == memory.id
        assert retrieved.node_type == NodeType.ATOMIC
        assert retrieved.importance == 7.0
        assert retrieved.is_current is True

    async def test_get_nonexistent(self, store: SQLiteStore):
        result = await store.get_memory_node("nonexistent-id")
        assert result is None

    async def test_entity_state_with_temporal_fields(self, store: SQLiteStore, memory: Memory):
        now = datetime.now(UTC)
        node = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ENTITY_STATE,
            is_current=False,
            valid_from=now - timedelta(days=7),
            valid_to=now,
            metadata={"entity_id": "ent-1", "state_key": "status", "value": "deployed"},
        )
        await store.save_memory_node(node)

        retrieved = await store.get_memory_node(node.id)
        assert retrieved is not None
        assert retrieved.node_type == NodeType.ENTITY_STATE
        assert retrieved.is_current is False
        assert retrieved.valid_from is not None
        assert retrieved.valid_to is not None
        assert retrieved.metadata["state_key"] == "status"

    async def test_get_by_type(self, store: SQLiteStore, memory: Memory):
        for nt in [NodeType.ATOMIC, NodeType.ATOMIC, NodeType.EPISODE]:
            await store.save_memory_node(MemoryNode(memory_id=memory.id, node_type=nt))

        atomics = await store.get_memory_nodes_by_type("atomic")
        episodes = await store.get_memory_nodes_by_type("episode")
        assert len(atomics) == 2
        assert len(episodes) == 1

    async def test_get_for_memory(self, store: SQLiteStore, memory: Memory):
        await store.save_memory_node(MemoryNode(memory_id=memory.id, node_type=NodeType.ATOMIC))
        await store.save_memory_node(
            MemoryNode(memory_id=memory.id, node_type=NodeType.ENTITY_STATE)
        )

        nodes = await store.get_memory_nodes_for_memory(memory.id)
        assert len(nodes) == 2
        types = {n.node_type for n in nodes}
        assert types == {NodeType.ATOMIC, NodeType.ENTITY_STATE}

    async def test_parent_id(self, store: SQLiteStore, memory: Memory):
        episode = MemoryNode(memory_id=memory.id, node_type=NodeType.EPISODE)
        await store.save_memory_node(episode)

        child = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ATOMIC,
            parent_id=episode.id,
        )
        await store.save_memory_node(child)

        retrieved = await store.get_memory_node(child.id)
        assert retrieved is not None
        assert retrieved.parent_id == episode.id


class TestGraphEdgeCRUD:
    async def test_save_and_get(self, store: SQLiteStore, memory: Memory):
        # Create two nodes first
        n1 = MemoryNode(memory_id=memory.id, node_type=NodeType.ATOMIC)
        n2 = MemoryNode(memory_id=memory.id, node_type=NodeType.EPISODE)
        await store.save_memory_node(n1)
        await store.save_memory_node(n2)

        edge = GraphEdge(
            source_id=n1.id,
            target_id=n2.id,
            edge_type=EdgeType.BELONGS_TO_EPISODE,
        )
        await store.save_graph_edge(edge)

        edges = await store.get_graph_edges(n1.id)
        assert len(edges) == 1
        assert edges[0].edge_type == EdgeType.BELONGS_TO_EPISODE
        assert edges[0].source_id == n1.id
        assert edges[0].target_id == n2.id

    async def test_filter_by_type(self, store: SQLiteStore, memory: Memory):
        n1 = MemoryNode(memory_id=memory.id, node_type=NodeType.ATOMIC)
        n2 = MemoryNode(memory_id=memory.id, node_type=NodeType.ATOMIC)
        await store.save_memory_node(n1)
        await store.save_memory_node(n2)

        await store.save_graph_edge(
            GraphEdge(source_id=n1.id, target_id=n2.id, edge_type=EdgeType.SUPPORTS)
        )
        await store.save_graph_edge(
            GraphEdge(source_id=n1.id, target_id=n2.id, edge_type=EdgeType.REFINES)
        )

        supports = await store.get_graph_edges(n1.id, edge_type="supports")
        refines = await store.get_graph_edges(n1.id, edge_type="refines")
        all_edges = await store.get_graph_edges(n1.id)

        assert len(supports) == 1
        assert len(refines) == 1
        assert len(all_edges) == 2

    async def test_edge_metadata(self, store: SQLiteStore, memory: Memory):
        n1 = MemoryNode(memory_id=memory.id, node_type=NodeType.ENTITY_STATE)
        n2 = MemoryNode(memory_id=memory.id, node_type=NodeType.ENTITY_STATE)
        await store.save_memory_node(n1)
        await store.save_memory_node(n2)

        edge = GraphEdge(
            source_id=n1.id,
            target_id=n2.id,
            edge_type=EdgeType.SUPERSEDES,
            weight=0.95,
            metadata={"reason": "version_bump"},
        )
        await store.save_graph_edge(edge)

        edges = await store.get_graph_edges(n1.id)
        assert edges[0].weight == 0.95
        assert edges[0].metadata["reason"] == "version_bump"

    async def test_empty_result(self, store: SQLiteStore):
        edges = await store.get_graph_edges("nonexistent")
        assert edges == []


class TestEphemeralCacheCRUD:
    async def test_save_and_get(self, store: SQLiteStore):
        now = datetime.now(UTC)
        entry = EphemeralEntry(
            content="transient info",
            source_agent="agent-1",
            domains=["frontend"],
            admission_score=0.35,
            expires_at=now + timedelta(hours=1),
        )
        await store.save_ephemeral(entry)

        retrieved = await store.get_ephemeral(entry.id)
        assert retrieved is not None
        assert retrieved.content == "transient info"
        assert retrieved.source_agent == "agent-1"
        assert retrieved.domains == ["frontend"]
        assert retrieved.admission_score == 0.35

    async def test_get_nonexistent(self, store: SQLiteStore):
        result = await store.get_ephemeral("nonexistent")
        assert result is None

    async def test_expire_removes_old_entries(self, store: SQLiteStore):
        past = datetime.now(UTC) - timedelta(hours=2)
        future = datetime.now(UTC) + timedelta(hours=2)

        expired_entry = EphemeralEntry(
            content="old", expires_at=past, created_at=past - timedelta(hours=1)
        )
        fresh_entry = EphemeralEntry(content="new", expires_at=future)

        await store.save_ephemeral(expired_entry)
        await store.save_ephemeral(fresh_entry)

        count = await store.expire_ephemeral()
        assert count == 1

        # Expired entry should be gone
        assert await store.get_ephemeral(expired_entry.id) is None
        # Fresh entry should remain
        assert await store.get_ephemeral(fresh_entry.id) is not None

    async def test_expire_returns_zero_when_nothing_expired(self, store: SQLiteStore):
        future = datetime.now(UTC) + timedelta(hours=2)
        entry = EphemeralEntry(content="still fresh", expires_at=future)
        await store.save_ephemeral(entry)

        count = await store.expire_ephemeral()
        assert count == 0
