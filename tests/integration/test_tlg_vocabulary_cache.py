"""TLG Phase 3b integration: L1 vocabulary cache + subject/entity lookup.

Verifies the cache:

* Builds ``InducedVocabulary`` from ENTITY_STATE nodes in the store.
* Memoises the result so subsequent calls are cheap.
* Rebuilds after :meth:`invalidate`.
* Returns the correct subject / entity for a query that references
  indexed tokens; returns ``None`` otherwise.
* Skips nodes without an ``entity_id`` metadata or without linked
  entities — both are legitimate no-subject signals.
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
    subject: str,
    linked_entity_ids: list[str],
    content: str = "state content",
) -> MemoryNode:
    mem = Memory(content=content, domains=["tlg-test"])
    await store.save_memory(mem)
    for eid in linked_entity_ids:
        await _ensure_entity(store, eid)
        await store.link_memory_entity(mem.id, eid)
    node = MemoryNode(
        memory_id=mem.id,
        node_type=NodeType.ENTITY_STATE,
        metadata={"entity_id": subject, "state_key": "k", "state_value": "v"},
    )
    await store.save_memory_node(node)
    return node


# ---------------------------------------------------------------------------


class TestBuildFromEntityStates:
    async def test_cold_store_produces_empty_vocab(self, store: SQLiteStore) -> None:
        cache = VocabularyCache()
        vocab = await cache.get_vocabulary(store)
        assert vocab.subject_lookup == {}
        assert vocab.entity_lookup == {}

    async def test_entity_state_nodes_contribute(self, store: SQLiteStore) -> None:
        await _seed_state(
            store,
            subject="auth-svc",
            linked_entity_ids=["session cookies", "OAuth 2.0"],
        )
        await _seed_state(
            store,
            subject="auth-svc",
            linked_entity_ids=["OAuth 2.0"],
        )
        cache = VocabularyCache()
        vocab = await cache.get_vocabulary(store)
        assert vocab.subject_lookup["session cookies"] == "auth-svc"
        assert vocab.subject_lookup["oauth 2.0"] == "auth-svc"

    async def test_nodes_without_entity_id_skipped(self, store: SQLiteStore) -> None:
        # A MemoryNode with ENTITY_STATE but no entity_id in metadata.
        mem = Memory(content="orphan state", domains=["tlg-test"])
        await store.save_memory(mem)
        await _ensure_entity(store, "orphan-ent")
        await store.link_memory_entity(mem.id, "orphan-ent")
        node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.ENTITY_STATE,
            metadata={"state_key": "k", "state_value": "v"},
        )
        await store.save_memory_node(node)

        cache = VocabularyCache()
        vocab = await cache.get_vocabulary(store)
        assert vocab.subject_lookup == {}

    async def test_nodes_without_linked_entities_skipped(self, store: SQLiteStore) -> None:
        mem = Memory(content="no-entity state", domains=["tlg-test"])
        await store.save_memory(mem)
        node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.ENTITY_STATE,
            metadata={"entity_id": "svc", "state_key": "k", "state_value": "v"},
        )
        await store.save_memory_node(node)

        cache = VocabularyCache()
        vocab = await cache.get_vocabulary(store)
        assert vocab.subject_lookup == {}


# ---------------------------------------------------------------------------


class TestCacheBehavior:
    async def test_get_vocabulary_memoises(self, store: SQLiteStore) -> None:
        await _seed_state(
            store,
            subject="svc",
            linked_entity_ids=["token-A"],
        )
        cache = VocabularyCache()
        v1 = await cache.get_vocabulary(store)
        v2 = await cache.get_vocabulary(store)
        # Same Python object — proof of memoisation.
        assert v1 is v2

    async def test_invalidate_forces_rebuild(self, store: SQLiteStore) -> None:
        await _seed_state(
            store,
            subject="svc",
            linked_entity_ids=["token-A"],
        )
        cache = VocabularyCache()
        v1 = await cache.get_vocabulary(store)

        # Add a new ENTITY_STATE node while the cache is warm.
        await _seed_state(
            store,
            subject="svc",
            linked_entity_ids=["token-B"],
        )

        # Before invalidate — cache still returns the pre-change result.
        v2 = await cache.get_vocabulary(store)
        assert v2 is v1
        assert "token-b" not in v2.entity_lookup

        # After invalidate — rebuilds to include the new data.
        cache.invalidate()
        v3 = await cache.get_vocabulary(store)
        assert v3 is not v1
        assert "token-b" in v3.entity_lookup


# ---------------------------------------------------------------------------


class TestLookups:
    async def test_lookup_subject_from_query(self, store: SQLiteStore) -> None:
        await _seed_state(
            store,
            subject="auth-svc",
            linked_entity_ids=["session cookies"],
        )
        cache = VocabularyCache()
        result = await cache.lookup_subject(
            "are we still using session cookies?",
            store,
        )
        assert result == "auth-svc"

    async def test_lookup_entity_returns_canonical_form(self, store: SQLiteStore) -> None:
        await _seed_state(
            store,
            subject="auth-svc",
            linked_entity_ids=["session cookies"],
        )
        cache = VocabularyCache()
        result = await cache.lookup_entity(
            "did we drop session cookies?",
            store,
        )
        assert result == "session cookies"

    async def test_no_match_returns_none(self, store: SQLiteStore) -> None:
        await _seed_state(
            store,
            subject="svc",
            linked_entity_ids=["foo"],
        )
        cache = VocabularyCache()
        assert await cache.lookup_subject("what's for lunch?", store) is None
        assert await cache.lookup_entity("what's for lunch?", store) is None

    async def test_cross_subject_ambiguity_resolves_to_majority(self, store: SQLiteStore) -> None:
        # "shared-token" appears in two subjects; auth-svc mentions it twice
        # vs gateway-svc once.  Majority wins.
        await _seed_state(
            store,
            subject="auth-svc",
            linked_entity_ids=["shared-token"],
        )
        await _seed_state(
            store,
            subject="auth-svc",
            linked_entity_ids=["shared-token"],
        )
        await _seed_state(
            store,
            subject="gateway-svc",
            linked_entity_ids=["shared-token"],
        )
        cache = VocabularyCache()
        assert await cache.lookup_subject("where is shared-token used?", store) == "auth-svc"
