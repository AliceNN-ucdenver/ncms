"""TLG alias expansion — integration test.

Verifies that a ``still`` query using the **short form** of an
entity (``"JWT"``) hits a SUPERSEDES edge that recorded the
**long form** (``"JSON Web Tokens"``) in its ``retires_entities``
set.  Without alias expansion this lookup would miss and fall back
to BM25.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.tlg import VocabularyCache
from ncms.domain.models import (
    Entity,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store() -> SQLiteStore:
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


async def _ensure_entity(store: SQLiteStore, eid: str) -> None:
    if await store.get_entity(eid) is not None:
        return
    ent = Entity(name=eid, type="concept")
    ent.id = eid
    await store.save_entity(ent)


async def _seed_state(
    store: SQLiteStore,
    *,
    content: str,
    entity_id: str,
    state_key: str,
    state_value: str,
    linked_entity_ids: list[str],
) -> MemoryNode:
    mem = Memory(content=content, domains=["tlg-alias-test"])
    await store.save_memory(mem)
    for eid in linked_entity_ids:
        await _ensure_entity(store, eid)
        await store.link_memory_entity(mem.id, eid)
    node = MemoryNode(
        memory_id=mem.id,
        node_type=NodeType.ENTITY_STATE,
        metadata={
            "entity_id": entity_id,
            "state_key": state_key,
            "state_value": state_value,
        },
    )
    await store.save_memory_node(node)
    return node


# NOTE: ``TestStillWithAlias`` was removed in the v6 cleanup — the
# dispatcher it exercised is no longer reachable via the SLM
# ``shape_intent_head`` taxonomy.  See docs for the v6 deletion
# rationale.


class TestAliasCacheInvalidation:
    async def test_aliases_cleared_on_invalidate(self, store: SQLiteStore) -> None:
        # Empty store
        cache = VocabularyCache()
        first = await cache.get_aliases(store)
        assert first == {}

        # Seed entities that form an alias pair.
        for eid in ["JWT", "JSON Web Tokens"]:
            await _ensure_entity(store, eid)
        mem = Memory(content="seed", domains=["t"])
        await store.save_memory(mem)
        await store.link_memory_entity(mem.id, "JWT")
        await store.link_memory_entity(mem.id, "JSON Web Tokens")
        node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.ENTITY_STATE,
            metadata={"entity_id": "svc", "state_key": "k", "state_value": "v"},
        )
        await store.save_memory_node(node)

        # Without invalidate, returns the cached empty result.
        still_empty = await cache.get_aliases(store)
        assert still_empty == {}

        # After invalidate, rebuild sees the new data.
        cache.invalidate()
        rebuilt = await cache.get_aliases(store)
        assert rebuilt, "cache rebuild did not pick up new aliases"
