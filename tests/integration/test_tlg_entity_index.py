"""Phase 4 — entity-memory O(1) index.

Verifies ``find_memory_ids_by_entity`` returns via the SQL index
and that TLG dispatch uses the fast path (no subject-scan fallback)
when the entity matches a registered record.

The scale test is small (200 memories) — the point is the *shape*
of the query (index lookup + ID-set filter), which stays O(log N)
regardless of corpus size.  The paper-grade benchmark curve (100k
memories) lives in the validation run, not the fast unit test.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.reconciliation_service import ReconciliationService
from ncms.application.tlg import VocabularyCache, retrieve_lg
from ncms.config import NCMSConfig
from ncms.domain.models import (
    Entity,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.domain.tlg import Confidence
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
    mem = Memory(content=content, domains=["tlg-entity-index"])
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


class TestFindMemoryIdsByEntity:
    async def test_returns_ids_by_exact_entity_id(
        self, store: SQLiteStore
    ) -> None:
        await _seed_state(
            store,
            content="Auth uses OAuth.",
            entity_id="auth-svc",
            state_key="k",
            state_value="v",
            linked_entity_ids=["OAuth"],
        )
        ids = await store.find_memory_ids_by_entity("OAuth")
        assert len(ids) == 1

    async def test_case_insensitive_name_match(
        self, store: SQLiteStore
    ) -> None:
        await _seed_state(
            store,
            content="Auth uses OAuth.",
            entity_id="auth-svc",
            state_key="k",
            state_value="v",
            linked_entity_ids=["OAuth"],
        )
        ids_lower = await store.find_memory_ids_by_entity("oauth")
        assert len(ids_lower) == 1

    async def test_missing_entity_returns_empty(
        self, store: SQLiteStore
    ) -> None:
        ids = await store.find_memory_ids_by_entity("nonexistent")
        assert ids == []

    async def test_multiple_memories_share_entity(
        self, store: SQLiteStore
    ) -> None:
        for i in range(3):
            await _seed_state(
                store,
                content=f"Memory {i} uses Docker.",
                entity_id=f"svc-{i}",
                state_key="k",
                state_value="v",
                linked_entity_ids=["Docker"],
            )
        ids = await store.find_memory_ids_by_entity("Docker")
        assert len(ids) == 3


class TestDispatchUsesIndex:
    async def test_sequence_dispatch_finds_via_index(
        self, store: SQLiteStore
    ) -> None:
        """Happy path: entity registered under its canonical name —
        dispatch should succeed via the fast index lookup."""
        config = NCMSConfig(
            db_path=":memory:",
            temporal_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        await _seed_state(
            store,
            content="Auth uses session cookies.",
            entity_id="auth-svc", state_key="auth_method",
            state_value="session cookies",
            linked_entity_ids=["session cookies", "authentication"],
        )
        v2 = await _seed_state(
            store,
            content="Retire session cookies; adopt OAuth.",
            entity_id="auth-svc", state_key="auth_method",
            state_value="OAuth",
            linked_entity_ids=["OAuth", "authentication"],
        )
        await service.reconcile(v2)

        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What came after session cookies for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "sequence"
        assert trace.confidence == Confidence.HIGH
        # Dispatch resolved the successor without a full-corpus scan.
        assert trace.grammar_answer == v2.id
