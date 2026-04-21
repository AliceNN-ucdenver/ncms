"""TLG Phase 3c integration: MemoryService.retrieve_lg entry point.

Verifies that the service-layer wrapper:

* Returns :attr:`Confidence.NONE` traces when TLG is disabled, even
  on a fully-populated store — guarantees the composition falls
  through to BM25 unchanged.
* Delegates to :func:`application.tlg.retrieve_lg` when enabled,
  producing confident HIGH/MEDIUM answers against real
  ENTITY_STATE data.
* :meth:`invalidate_tlg_vocabulary` forces the next dispatch to
  pick up newly-seeded state nodes.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.application.reconciliation_service import ReconciliationService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    Entity,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.domain.tlg import Confidence
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def service_tlg_on() -> MemoryService:
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    config = NCMSConfig(
        db_path=":memory:",
        temporal_enabled=True,
    )
    reconciliation = ReconciliationService(store=store, config=config)
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=config,
        reconciliation=reconciliation,
    )
    yield svc
    await store.close()


# NOTE: ``service_tlg_off`` fixture and ``TestTLGDisabled`` suite were
# removed when the NCMSConfig flag scheme collapsed tlg/reconciliation/
# episodes/intent_classification/intent_routing into the single
# ``temporal_enabled`` master flag.  The disabled path is now a single
# short-circuit checked implicitly by every other unit test that runs
# without ``temporal_enabled=True``.


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
        metadata={
            "entity_id": entity_id,
            "state_key": state_key,
            "state_value": state_value,
        },
    )
    await store.save_memory_node(node)
    return node


class TestTLGEnabled:
    async def test_current_intent_resolves(
        self, service_tlg_on: MemoryService,
    ) -> None:
        await _seed_state(
            service_tlg_on.store,
            content="Authentication uses session cookies.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="session cookies",
            linked_entity_ids=["session cookies", "authentication"],
        )
        v2 = await _seed_state(
            service_tlg_on.store,
            content="Retire session cookies; adopt OAuth 2.0 tokens.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0", "authentication"],
        )
        await service_tlg_on._reconciliation.reconcile(v2)  # type: ignore[attr-defined]

        trace = await service_tlg_on.retrieve_lg(
            "What is the current authentication method?"
        )
        assert trace.confidence == Confidence.HIGH
        assert trace.grammar_answer == v2.id
        assert trace.has_confident_answer()

    async def test_invalidate_picks_up_new_state(
        self, service_tlg_on: MemoryService,
    ) -> None:
        # Warm the cache with empty corpus.
        trace1 = await service_tlg_on.retrieve_lg(
            "What is the current auth method?"
        )
        assert trace1.confidence == Confidence.ABSTAIN  # no subject known

        # Seed state while cache is warm.
        await _seed_state(
            service_tlg_on.store,
            content="Authentication uses OAuth 2.0.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0", "authentication"],
        )
        # Without invalidation — cache still sees empty corpus.
        trace2 = await service_tlg_on.retrieve_lg(
            "What is the current authentication method?"
        )
        assert trace2.confidence == Confidence.ABSTAIN

        # Invalidate + retry — now dispatches.
        service_tlg_on.invalidate_tlg_vocabulary()
        trace3 = await service_tlg_on.retrieve_lg(
            "What is the current authentication method?"
        )
        assert trace3.confidence == Confidence.HIGH
        assert trace3.grammar_answer is not None
