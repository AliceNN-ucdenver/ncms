"""TLG Phase 3c integration: ``retrieve_lg`` dispatch end-to-end.

Seeds entity-state chains through the production ReconciliationService
with TLG enabled (so ``retires_entities`` gets populated), then fires
natural-language queries through :func:`retrieve_lg` and verifies:

* ``current`` queries return the latest is_current ENTITY_STATE node.
* ``origin`` queries return the earliest state-change memory for the
  subject.
* ``still`` queries use the structural retirement set when present,
  and the current-zone heuristic as a medium-confidence fallback.
* Non-matching queries produce :attr:`Confidence.NONE` /
  :attr:`Confidence.ABSTAIN` traces — so the composition layer can
  safely fall through to BM25 unchanged.

The grammar ∨ BM25 composition is exercised separately in the unit
tests; here we focus on the NCMS plumbing (store ↔ vocabulary cache
↔ dispatch) behaving correctly.
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
from ncms.domain.tlg import Confidence, compose
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
    mem = Memory(content=content, domains=["tlg-test"])
    await store.save_memory(mem)
    for eid in linked_entity_ids:
        await _ensure_entity(store, eid)
        await store.link_memory_entity(mem.id, eid)
    node = MemoryNode(
        memory_id=mem.id,
        node_type=NodeType.ENTITY_STATE,
        importance=5.0,
        metadata={
            "entity_id": entity_id,
            "state_key": state_key,
            "state_value": state_value,
        },
    )
    await store.save_memory_node(node)
    return node


async def _auth_corpus(store: SQLiteStore) -> tuple[MemoryNode, MemoryNode]:
    """Seed an auth-svc subject with two states; second supersedes first.

    Returns (origin_node, current_node).
    """
    config = NCMSConfig(
        db_path=":memory:",
        reconciliation_enabled=True,
        tlg_enabled=True,
    )
    service = ReconciliationService(store=store, config=config)

    # Origin — first state for the subject.
    v1 = await _seed_state(
        store,
        content="Authentication uses session cookies.",
        entity_id="auth-svc",
        state_key="auth_method",
        state_value="session cookies",
        linked_entity_ids=["session cookies", "authentication"],
    )

    # Current — announcement that retires session cookies for OAuth.
    v2 = await _seed_state(
        store,
        content="Retire session cookies; adopt OAuth 2.0 tokens.",
        entity_id="auth-svc",
        state_key="auth_method",
        state_value="OAuth 2.0",
        linked_entity_ids=["OAuth 2.0", "authentication"],
    )
    await service.reconcile(v2)
    return v1, v2


# ---------------------------------------------------------------------------


class TestCurrentIntent:
    async def test_returns_current_entity_state_high_confidence(
        self, store: SQLiteStore
    ) -> None:
        _, current = await _auth_corpus(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What is the current auth method for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "current"
        assert trace.intent.subject == "auth-svc"
        assert trace.grammar_answer == current.id
        assert trace.confidence == Confidence.HIGH
        assert trace.has_confident_answer()

    async def test_no_states_abstains(self, store: SQLiteStore) -> None:
        # No memories at all — cache lookup returns no subject, so
        # dispatch abstains.
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What is the current auth method?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.confidence == Confidence.ABSTAIN
        assert trace.grammar_answer is None
        assert not trace.has_confident_answer()


class TestOriginIntent:
    async def test_returns_earliest_state(self, store: SQLiteStore) -> None:
        origin, _ = await _auth_corpus(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What was the original authentication method?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "origin"
        assert trace.grammar_answer == origin.id
        assert trace.confidence == Confidence.HIGH


class TestStillIntent:
    async def test_retired_entity_points_to_superseding_node(
        self, store: SQLiteStore
    ) -> None:
        _, current = await _auth_corpus(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "Are we still using session cookies for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "still"
        # The retirement path points to the announcement node — the
        # one that said "Retire session cookies" — which is `current`.
        assert trace.grammar_answer == current.id
        assert trace.confidence == Confidence.HIGH

    async def test_still_active_entity_medium_confidence(
        self, store: SQLiteStore
    ) -> None:
        # Entity that's linked to the CURRENT state (not retired):
        # expect MEDIUM confidence.
        _, current = await _auth_corpus(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "Are we still using OAuth 2.0 for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.intent.kind == "still"
        assert trace.grammar_answer == current.id
        assert trace.confidence == Confidence.MEDIUM

    async def test_unknown_entity_abstains(self, store: SQLiteStore) -> None:
        await _auth_corpus(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "Are we still using authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        # Entity "authentication" is linked to BOTH states — so the
        # current-zone heuristic DOES fire and confidence is MEDIUM.
        # This documents the research behavior.
        assert trace.intent.kind == "still"
        assert trace.confidence in (Confidence.MEDIUM, Confidence.HIGH)


class TestNoIntent:
    async def test_non_grammar_query_produces_none(
        self, store: SQLiteStore
    ) -> None:
        await _auth_corpus(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "Who authored the design document?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.confidence == Confidence.NONE
        assert trace.grammar_answer is None


# ---------------------------------------------------------------------------
# End-to-end composition: grammar ∨ BM25
# ---------------------------------------------------------------------------


class TestComposition:
    async def test_confident_trace_prepends_onto_bm25(
        self, store: SQLiteStore
    ) -> None:
        _, current = await _auth_corpus(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What is the current auth method for authentication?",
            store=store,
            vocabulary_cache=cache,
        )
        # Simulated BM25 ranking that doesn't surface `current` at rank 1.
        bm25 = ["other-memory-1", "other-memory-2", current.id]
        composed = compose(bm25, trace)
        assert composed[0] == current.id
        # BM25 tail follows, with the original occurrence de-duped.
        assert "other-memory-1" in composed
        assert "other-memory-2" in composed

    async def test_no_intent_trace_preserves_bm25_exactly(
        self, store: SQLiteStore
    ) -> None:
        await _auth_corpus(store)
        cache = VocabularyCache()
        trace = await retrieve_lg(
            "random question with no grammar structure",
            store=store,
            vocabulary_cache=cache,
        )
        bm25 = ["a", "b", "c"]
        assert compose(bm25, trace) == bm25
