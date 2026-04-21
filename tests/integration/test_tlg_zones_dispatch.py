"""TLG zones in dispatch — multi-zone subjects and refines chains.

Complements :mod:`test_tlg_dispatch` which covers simple single-node
cases.  These tests exercise the zone walker against graphs produced
by the actual ReconciliationService:

* ``current`` spans a ``refines`` chain (not just the newest node).
* ``origin`` returns the root of the earliest zone across multiple
  supersession hops.
* ``still`` uses the zone-scoped retirement lookup (with stem /
  alias / prefix matching) for entities retired multiple zones ago.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.reconciliation_service import ReconciliationService
from ncms.application.tlg import VocabularyCache, retrieve_lg
from ncms.config import NCMSConfig
from ncms.domain.models import (
    EdgeType,
    Entity,
    GraphEdge,
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
    mem = Memory(content=content, domains=["tlg-zones-test"])
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


class TestRefinesChain:
    async def test_current_walks_refines_chain(
        self, store: SQLiteStore
    ) -> None:
        """A zone with a refines chain: current is the chain terminal,
        zone_context is the earlier chain links."""
        # Seed three nodes in a refines chain.  We create the REFINES
        # edges manually (reconciliation doesn't auto-emit them for
        # this test).
        a = await _seed_state(
            store,
            content="Auth uses OAuth 2.0.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0", "authentication"],
        )
        b = await _seed_state(
            store,
            content="Auth now requires MFA on top of OAuth.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0 + MFA",
            linked_entity_ids=["OAuth 2.0", "MFA", "authentication"],
        )
        c = await _seed_state(
            store,
            content="Auth MFA now enforces hardware keys.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0 + MFA + hardware keys",
            linked_entity_ids=["MFA", "hardware keys", "authentication"],
        )
        # NCMS REFINES stores source=new, target=existing.
        # zones._load_subject_zones inverts this to old→new for
        # the zone walker.
        await store.save_graph_edge(GraphEdge(
            source_id=b.id, target_id=a.id, edge_type=EdgeType.REFINES,
        ))
        await store.save_graph_edge(GraphEdge(
            source_id=c.id, target_id=b.id, edge_type=EdgeType.REFINES,
        ))

        cache = VocabularyCache()
        trace = await retrieve_lg(
            "What is the current authentication method?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.confidence == Confidence.HIGH
        # Terminal of the chain = c (it was refined from b from a).
        assert trace.grammar_answer == c.id
        # zone_context contains a and b (chain members minus terminal).
        assert set(trace.zone_context) == {a.id, b.id}


class TestMultiZoneSupersession:
    async def test_origin_is_earliest_zone_root_across_supersessions(
        self, store: SQLiteStore
    ) -> None:
        """Three zones: v1 -> (supersedes) v2 -> (supersedes) v3.
        ``origin`` should return v1 even though reconciliation has
        closed it twice."""
        config = NCMSConfig(
            db_path=":memory:",
            temporal_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        v1 = await _seed_state(
            store,
            content="Auth uses session cookies.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="session cookies",
            linked_entity_ids=["session cookies", "authentication"],
        )
        v2 = await _seed_state(
            store,
            content="Retire session cookies; adopt OAuth 2.0.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0", "authentication"],
        )
        await service.reconcile(v2)

        v3 = await _seed_state(
            store,
            content="Retire OAuth 2.0; adopt WebAuthn passkeys.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="WebAuthn",
            linked_entity_ids=["WebAuthn", "authentication"],
        )
        await service.reconcile(v3)

        cache = VocabularyCache()
        origin_trace = await retrieve_lg(
            "What was the original authentication method?",
            store=store,
            vocabulary_cache=cache,
        )
        assert origin_trace.confidence == Confidence.HIGH
        assert origin_trace.grammar_answer == v1.id

        current_trace = await retrieve_lg(
            "What is the current authentication method?",
            store=store,
            vocabulary_cache=cache,
        )
        assert current_trace.confidence == Confidence.HIGH
        assert current_trace.grammar_answer == v3.id

    async def test_still_handles_multi_hop_retirement(
        self, store: SQLiteStore
    ) -> None:
        """After two supersessions, 'still using session cookies?' must
        still resolve the retirement via the zone-scoped extractor."""
        config = NCMSConfig(
            db_path=":memory:",
            temporal_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        await _seed_state(
            store,
            content="Auth uses session cookies.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="session cookies",
            linked_entity_ids=["session cookies", "authentication"],
        )
        v2 = await _seed_state(
            store,
            content="Retire session cookies; adopt OAuth 2.0.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0", "authentication"],
        )
        await service.reconcile(v2)

        v3 = await _seed_state(
            store,
            content="Retire OAuth 2.0; adopt WebAuthn passkeys.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="WebAuthn",
            linked_entity_ids=["WebAuthn", "authentication"],
        )
        await service.reconcile(v3)

        cache = VocabularyCache()
        trace = await retrieve_lg(
            "Are we still using session cookies for auth?",
            store=store,
            vocabulary_cache=cache,
        )
        assert trace.confidence == Confidence.HIGH
        # The retirement extractor finds the supersession closest to
        # session cookies, which is v2 (where they were retired).
        assert trace.grammar_answer == v2.id
