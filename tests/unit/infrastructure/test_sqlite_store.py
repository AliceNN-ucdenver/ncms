"""Tests for SQLite storage backend."""

import pytest
import pytest_asyncio

from ncms.domain.models import (
    AccessRecord,
    Entity,
    KnowledgePayload,
    KnowledgeSnapshot,
    Memory,
    Relationship,
    SnapshotEntry,
)
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def db():
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


class TestMemoryCRUD:
    @pytest.mark.asyncio
    async def test_save_and_get(self, db: SQLiteStore):
        """Save and retrieve a memory, verifying all fields round-trip."""
        m = Memory(
            content="test content",
            domains=["api", "auth"],
            type="interface-spec",
            importance=7.5,
            source_agent="agent-1",
        )
        await db.save_memory(m)
        loaded = await db.get_memory(m.id)
        assert loaded is not None
        assert loaded.id == m.id
        assert loaded.content == m.content
        assert loaded.domains == m.domains
        assert loaded.type == m.type
        assert loaded.importance == m.importance
        assert loaded.source_agent == m.source_agent

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db: SQLiteStore):
        result = await db.get_memory("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_memory(self, db: SQLiteStore):
        """Updating a memory should persist the changes."""
        m = Memory(content="original content", domains=["api"])
        await db.save_memory(m)

        m.content = "updated content"
        m.domains = ["api", "v2"]
        await db.update_memory(m)

        loaded = await db.get_memory(m.id)
        assert loaded is not None
        assert loaded.content == "updated content"
        assert set(loaded.domains) == {"api", "v2"}

    @pytest.mark.asyncio
    async def test_delete(self, db: SQLiteStore):
        m = Memory(content="to delete")
        await db.save_memory(m)
        await db.delete_memory(m.id)
        assert await db.get_memory(m.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_safe(self, db: SQLiteStore):
        """Deleting a nonexistent memory should not raise."""
        await db.delete_memory("does-not-exist")

    @pytest.mark.asyncio
    async def test_list_by_domain(self, db: SQLiteStore):
        """Listing by domain should only return memories in that domain."""
        m_api = Memory(content="api stuff", domains=["api"])
        m_db = Memory(content="db stuff", domains=["db"])
        await db.save_memory(m_api)
        await db.save_memory(m_db)

        api_memories = await db.list_memories(domain="api")
        assert len(api_memories) == 1
        assert api_memories[0].id == m_api.id

        db_memories = await db.list_memories(domain="db")
        assert len(db_memories) == 1
        assert db_memories[0].id == m_db.id

    @pytest.mark.asyncio
    async def test_list_by_agent(self, db: SQLiteStore):
        """Listing by agent should only return that agent's memories."""
        m_a = Memory(content="a1", source_agent="agent-a")
        m_b = Memory(content="b1", source_agent="agent-b")
        await db.save_memory(m_a)
        await db.save_memory(m_b)

        a_memories = await db.list_memories(agent_id="agent-a")
        assert len(a_memories) == 1
        assert a_memories[0].id == m_a.id

    @pytest.mark.asyncio
    async def test_list_all(self, db: SQLiteStore):
        """Listing without filters should return all memories."""
        for i in range(3):
            await db.save_memory(Memory(content=f"memory {i}"))
        all_mems = await db.list_memories()
        assert len(all_mems) == 3

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, db: SQLiteStore):
        """List should respect the limit parameter."""
        for i in range(5):
            await db.save_memory(Memory(content=f"memory {i}"))
        limited = await db.list_memories(limit=2)
        assert len(limited) == 2


class TestAccessLog:
    @pytest.mark.asyncio
    async def test_log_and_retrieve(self, db: SQLiteStore):
        """Access times should be recorded and retrievable."""
        m = Memory(content="test")
        await db.save_memory(m)

        for _ in range(3):
            await db.log_access(AccessRecord(memory_id=m.id, accessing_agent="test"))

        ages = await db.get_access_times(m.id)
        assert len(ages) == 3
        assert all(isinstance(a, float) and a >= 0 for a in ages)

    @pytest.mark.asyncio
    async def test_no_accesses_returns_empty(self, db: SQLiteStore):
        """Memory with no access records should return empty list."""
        m = Memory(content="unaccessed")
        await db.save_memory(m)
        ages = await db.get_access_times(m.id)
        assert ages == []


class TestEntityCRUD:
    @pytest.mark.asyncio
    async def test_save_and_get(self, db: SQLiteStore):
        e = Entity(name="UserService", type="service")
        await db.save_entity(e)
        loaded = await db.get_entity(e.id)
        assert loaded is not None
        assert loaded.name == e.name
        assert loaded.type == e.type

    @pytest.mark.asyncio
    async def test_get_nonexistent_entity(self, db: SQLiteStore):
        result = await db.get_entity("no-such-entity")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_by_name(self, db: SQLiteStore):
        e = Entity(name="ProfileEndpoint", type="endpoint")
        await db.save_entity(e)
        found = await db.find_entity_by_name("ProfileEndpoint")
        assert found is not None
        assert found.id == e.id

    @pytest.mark.asyncio
    async def test_find_case_insensitive(self, db: SQLiteStore):
        e = Entity(name="UserTable", type="table")
        await db.save_entity(e)
        found = await db.find_entity_by_name("usertable")
        assert found is not None
        assert found.id == e.id

    @pytest.mark.asyncio
    async def test_find_nonexistent_by_name(self, db: SQLiteStore):
        found = await db.find_entity_by_name("DoesNotExist")
        assert found is None

    @pytest.mark.asyncio
    async def test_list_entities(self, db: SQLiteStore):
        """List entities should return all entities, optionally filtered by type."""
        await db.save_entity(Entity(name="Svc1", type="service"))
        await db.save_entity(Entity(name="EP1", type="endpoint"))
        await db.save_entity(Entity(name="Svc2", type="service"))

        all_entities = await db.list_entities()
        assert len(all_entities) == 3

        services = await db.list_entities(entity_type="service")
        assert len(services) == 2
        assert all(e.type == "service" for e in services)


class TestRelationshipCRUD:
    @pytest.mark.asyncio
    async def test_save_and_get_relationships(self, db: SQLiteStore):
        """Saving a relationship should make it retrievable by entity id."""
        e1 = Entity(name="A", type="service")
        e2 = Entity(name="B", type="endpoint")
        await db.save_entity(e1)
        await db.save_entity(e2)

        rel = Relationship(
            source_entity_id=e1.id,
            target_entity_id=e2.id,
            type="exposes",
        )
        await db.save_relationship(rel)

        rels = await db.get_relationships(e1.id)
        assert len(rels) == 1
        assert rels[0].source_entity_id == e1.id
        assert rels[0].target_entity_id == e2.id
        assert rels[0].type == "exposes"

    @pytest.mark.asyncio
    async def test_no_relationships(self, db: SQLiteStore):
        """Entity with no relationships should return empty list."""
        e = Entity(name="Lonely", type="service")
        await db.save_entity(e)
        rels = await db.get_relationships(e.id)
        assert rels == []


class TestSnapshots:
    @pytest.mark.asyncio
    async def test_save_and_get(self, db: SQLiteStore):
        entry = SnapshotEntry(
            domain="api",
            knowledge=KnowledgePayload(content="endpoint spec"),
            confidence=0.85,
        )
        snap = KnowledgeSnapshot(
            agent_id="test-agent",
            domains=["api"],
            entries=[entry],
        )
        await db.save_snapshot(snap)
        loaded = await db.get_latest_snapshot("test-agent")
        assert loaded is not None
        assert loaded.agent_id == snap.agent_id
        assert loaded.snapshot_id == snap.snapshot_id
        assert len(loaded.entries) == 1
        assert loaded.entries[0].knowledge.content == entry.knowledge.content
        assert loaded.entries[0].confidence == entry.confidence

    @pytest.mark.asyncio
    async def test_latest_snapshot_is_most_recent(self, db: SQLiteStore):
        """When multiple snapshots exist, get_latest should return the newest."""
        snap1 = KnowledgeSnapshot(
            agent_id="test-agent",
            domains=["api"],
            entries=[
                SnapshotEntry(domain="api", knowledge=KnowledgePayload(content="v1")),
            ],
        )
        await db.save_snapshot(snap1)

        snap2 = KnowledgeSnapshot(
            agent_id="test-agent",
            domains=["api"],
            entries=[
                SnapshotEntry(domain="api", knowledge=KnowledgePayload(content="v2")),
            ],
        )
        await db.save_snapshot(snap2)

        loaded = await db.get_latest_snapshot("test-agent")
        assert loaded is not None
        assert loaded.snapshot_id == snap2.snapshot_id
        assert loaded.entries[0].knowledge.content == "v2"

    @pytest.mark.asyncio
    async def test_delete(self, db: SQLiteStore):
        snap = KnowledgeSnapshot(agent_id="del-agent", domains=[])
        await db.save_snapshot(snap)
        await db.delete_snapshot("del-agent")
        assert await db.get_latest_snapshot("del-agent") is None

    @pytest.mark.asyncio
    async def test_get_nonexistent_snapshot(self, db: SQLiteStore):
        result = await db.get_latest_snapshot("no-such-agent")
        assert result is None


class TestConsolidationState:
    @pytest.mark.asyncio
    async def test_set_and_get(self, db: SQLiteStore):
        """Consolidation state should persist key-value pairs."""
        await db.set_consolidation_value("last_decay_run", "2024-01-01T00:00:00Z")
        val = await db.get_consolidation_value("last_decay_run")
        assert val == "2024-01-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_overwrite(self, db: SQLiteStore):
        """Setting the same key again should overwrite."""
        await db.set_consolidation_value("key", "v1")
        await db.set_consolidation_value("key", "v2")
        val = await db.get_consolidation_value("key")
        assert val == "v2"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db: SQLiteStore):
        val = await db.get_consolidation_value("nonexistent")
        assert val is None


class TestMemoryEntityLinks:
    @pytest.mark.asyncio
    async def test_link_and_retrieve(self, db: SQLiteStore):
        m = Memory(content="test")
        e = Entity(name="Thing", type="concept")
        await db.save_memory(m)
        await db.save_entity(e)
        await db.link_memory_entity(m.id, e.id)

        entities = await db.get_memory_entities(m.id)
        assert e.id in entities

    @pytest.mark.asyncio
    async def test_multiple_links(self, db: SQLiteStore):
        """A memory can be linked to multiple entities."""
        m = Memory(content="multi-entity")
        e1 = Entity(name="A", type="concept")
        e2 = Entity(name="B", type="concept")
        await db.save_memory(m)
        await db.save_entity(e1)
        await db.save_entity(e2)
        await db.link_memory_entity(m.id, e1.id)
        await db.link_memory_entity(m.id, e2.id)

        entities = await db.get_memory_entities(m.id)
        assert set(entities) == {e1.id, e2.id}

    @pytest.mark.asyncio
    async def test_no_links_returns_empty(self, db: SQLiteStore):
        m = Memory(content="unlinked")
        await db.save_memory(m)
        entities = await db.get_memory_entities(m.id)
        assert entities == []
