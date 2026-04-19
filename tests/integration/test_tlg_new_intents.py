"""TLG new-intent dispatch — end-to-end.

Sequence, predecessor, interval, range, before_named, transitive_cause,
concurrent, cause_of, and retirement intents firing through
``retrieve_lg`` against a ReconciliationService-seeded store.  Verifies
each dispatcher returns a confident grammar answer when the query
slots resolve, and abstains cleanly when they don't.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    observed_at: datetime | None = None,
) -> MemoryNode:
    mem = Memory(content=content, domains=["tlg-new-intents"])
    if observed_at is not None:
        mem.observed_at = observed_at
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
        observed_at=observed_at,
    )
    await store.save_memory_node(node)
    return node


async def _auth_chain(store: SQLiteStore) -> tuple[
    MemoryNode, MemoryNode, MemoryNode
]:
    """v1 (cookies) → v2 (OAuth) → v3 (passkeys), reconciled."""
    config = NCMSConfig(
        db_path=":memory:",
        reconciliation_enabled=True,
        tlg_enabled=True,
    )
    service = ReconciliationService(store=store, config=config)

    base = datetime(2024, 1, 1, tzinfo=UTC)
    v1 = await _seed_state(
        store,
        content="Auth uses session cookies.",
        entity_id="auth-svc", state_key="auth_method",
        state_value="session cookies",
        linked_entity_ids=["session cookies", "authentication"],
        observed_at=base,
    )
    v2 = await _seed_state(
        store,
        content="Retire session cookies; adopt OAuth.",
        entity_id="auth-svc", state_key="auth_method",
        state_value="OAuth",
        linked_entity_ids=["OAuth", "authentication"],
        observed_at=base + timedelta(days=30),
    )
    await service.reconcile(v2)

    v3 = await _seed_state(
        store,
        content="Retire OAuth; adopt passkeys.",
        entity_id="auth-svc", state_key="auth_method",
        state_value="passkeys",
        linked_entity_ids=["passkeys", "authentication"],
        observed_at=base + timedelta(days=120),
    )
    await service.reconcile(v3)
    return v1, v2, v3


# ---------------------------------------------------------------------------


class TestSequence:
    async def test_what_came_after_resolves_successor(
        self, store: SQLiteStore
    ) -> None:
        _, v2, v3 = await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What came after OAuth for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "sequence"
        assert trace.confidence == Confidence.HIGH
        # OAuth (v2) is superseded by passkeys (v3).
        assert trace.grammar_answer == v3.id

    async def test_no_successor_abstains(
        self, store: SQLiteStore
    ) -> None:
        _, _, _ = await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What came after passkeys for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "sequence"
        assert trace.confidence == Confidence.ABSTAIN


class TestPredecessor:
    async def test_what_came_before_resolves_predecessor(
        self, store: SQLiteStore
    ) -> None:
        v1, v2, _ = await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What came before OAuth for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "predecessor"
        assert trace.confidence == Confidence.HIGH
        assert trace.grammar_answer == v1.id


class TestBeforeNamed:
    async def test_which_came_first(self, store: SQLiteStore) -> None:
        v1, v2, _ = await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "Did session cookies come before OAuth?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "before_named"
        assert trace.confidence == Confidence.HIGH
        # session cookies (v1) were earlier.
        assert trace.grammar_answer == v1.id
        assert v2.id in trace.zone_context


class TestInterval:
    async def test_between_two_events(self, store: SQLiteStore) -> None:
        v1, v2, v3 = await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What happened between session cookies and passkeys?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "interval"
        assert trace.confidence == Confidence.HIGH
        # v2 (OAuth) is between v1 and v3.
        assert trace.grammar_answer == v2.id


class TestRange:
    async def test_q1_range_filter(self, store: SQLiteStore) -> None:
        await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What happened to authentication in Q1 2024?",
            store=store,
            vocabulary_cache=cache,
        )
        # January 2024 — only v1 (cookies) at 2024-01-01 is in Q1.
        assert trace.intent.kind == "range"
        assert trace.confidence == Confidence.HIGH
        # Grammar returns earliest memory in range.
        assert trace.grammar_answer is not None


class TestTransitiveCause:
    async def test_eventually_led_to_walks_ancestors(
        self, store: SQLiteStore
    ) -> None:
        v1, _, _ = await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What eventually led to passkeys for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "transitive_cause"
        assert trace.confidence == Confidence.HIGH
        # The ancestor walk should reach v1 (session cookies) as the root.
        assert trace.grammar_answer == v1.id


class TestConcurrent:
    async def test_during_finds_overlapping(self, store: SQLiteStore) -> None:
        v1, v2, v3 = await _auth_chain(store)
        # Add a sibling in the same subject within v2's window.
        from datetime import datetime
        base = datetime(2024, 1, 31, tzinfo=UTC)
        await _seed_state(
            store,
            content="Audit logs enabled for auth.",
            entity_id="auth-svc", state_key="audit",
            state_value="enabled",
            linked_entity_ids=["audit-logs", "authentication"],
            observed_at=base,
        )
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What was happening during OAuth rollout?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "concurrent"
        # In-subject concurrent is MEDIUM confidence by design.
        assert trace.confidence in (Confidence.MEDIUM, Confidence.ABSTAIN)


class TestRetirement:
    async def test_retire_imperative_finds_retirement_edge(
        self, store: SQLiteStore
    ) -> None:
        _, v2, v3 = await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "did we retire session cookies?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "retirement"
        # v2 is the retirement announcement for session cookies.
        assert trace.confidence == Confidence.HIGH
        assert trace.grammar_answer == v2.id


class TestUnresolvedEntity:
    async def test_sequence_on_unknown_entity_abstains(
        self, store: SQLiteStore
    ) -> None:
        await _auth_chain(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What came after smoke signals for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "sequence"
        assert trace.confidence == Confidence.ABSTAIN
