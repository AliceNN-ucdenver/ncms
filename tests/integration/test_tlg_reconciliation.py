"""TLG Phase 1 integration: reconciliation populates retires_entities.

End-to-end check that when ``NCMS_TEMPORAL_ENABLED=true`` a SUPERSEDES
reconciliation path produces a ``graph_edges.retires_entities`` set
derived from the structural extractor.  With the flag off, the edge
is emitted with an empty set — preserving pre-TLG behavior.

Covers Phase 1 wiring only.  Seed verbs from
``SEED_RETIREMENT_VERBS`` are sufficient until Phase 2 induction
populates ``grammar_transition_markers``.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.reconciliation_service import ReconciliationService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    EdgeType,
    Entity,
    Memory,
    MemoryNode,
    NodeType,
    RelationType,
)
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store() -> SQLiteStore:
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


async def _ensure_entity(store: SQLiteStore, entity_id: str) -> None:
    """FK-safe: seed an Entity row if one doesn't already exist."""
    existing = await store.get_entity(entity_id)
    if existing is not None:
        return
    ent = Entity(name=entity_id, type="concept")
    ent.id = entity_id
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
    """Create a Memory (with linked entities) + an entity-state MemoryNode."""
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


# NOTE: The "TLG flag off while reconciliation on" sub-phase ablation was
# removed when the NCMSConfig flag scheme collapsed tlg/reconciliation/
# episodes/intent_classification/intent_routing into the single
# ``temporal_enabled`` master flag.  All TLG behaviour is now tested under
# ``temporal_enabled=True``.


class TestTLGFlagOn:
    async def test_structural_extraction_populates_retires(
        self, store: SQLiteStore
    ) -> None:
        config = NCMSConfig(
            db_path=":memory:",
            temporal_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        # Old state: session cookies are the current auth method.
        await _seed_state(
            store,
            content="Authentication uses session cookies.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="session cookies",
            linked_entity_ids=["session cookies"],
        )
        # New state: announcement retires session cookies.
        v2 = await _seed_state(
            store,
            content="Retire session cookies; adopt OAuth 2.0 tokens.",
            entity_id="auth-svc",
            state_key="auth_method",
            state_value="OAuth 2.0",
            linked_entity_ids=["OAuth 2.0"],
        )

        results = await service.reconcile(v2)
        assert len(results) == 1
        assert results[0].relation == RelationType.SUPERSEDES

        # Both edges carry the structurally-extracted retires set.
        supersedes = await store.get_graph_edges(v2.id, EdgeType.SUPERSEDES)
        assert len(supersedes) == 1
        assert "session cookies" in supersedes[0].retires_entities

        # Reverse SUPERSEDED_BY edge carries the same set.
        superseded_by = await store.get_graph_edges(
            supersedes[0].target_id, EdgeType.SUPERSEDED_BY,
        )
        assert len(superseded_by) == 1
        assert superseded_by[0].retires_entities == supersedes[0].retires_entities

    async def test_setdiff_safety_net_on_silent_drop(
        self, store: SQLiteStore
    ) -> None:
        """No retirement verb in content, but old entity disappears.

        The extractor's set-diff tail must still catch the drop so
        ``retires_entities`` isn't silently empty for a real
        supersession.
        """
        config = NCMSConfig(
            db_path=":memory:",
            temporal_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        await _seed_state(
            store,
            content="Backend uses legacy-config module.",
            entity_id="backend",
            state_key="config",
            state_value="legacy-config",
            linked_entity_ids=["legacy-config"],
        )
        v2 = await _seed_state(
            store,
            content="Backend now reads feature flags at startup.",
            entity_id="backend",
            state_key="config",
            state_value="feature flags",
            linked_entity_ids=["feature flags"],
        )

        await service.reconcile(v2)
        edges = await store.get_graph_edges(v2.id, EdgeType.SUPERSEDES)
        assert len(edges) == 1
        # legacy-config was in src entities, not in dst — set-diff fires.
        assert "legacy-config" in edges[0].retires_entities

    async def test_missing_memory_does_not_raise(
        self, store: SQLiteStore
    ) -> None:
        """If a memory row is missing, extraction returns empty list
        rather than raising.  Reconciliation semantics are preserved.
        """
        config = NCMSConfig(
            db_path=":memory:",
            temporal_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        v1 = await _seed_state(
            store,
            content="",
            entity_id="svc",
            state_key="status",
            state_value="starting",
            linked_entity_ids=[],
        )
        v2 = await _seed_state(
            store,
            content="",
            entity_id="svc",
            state_key="status",
            state_value="running",
            linked_entity_ids=[],
        )

        # Normal reconcile still works — just with empty retires.
        results = await service.reconcile(v2)
        assert len(results) == 1
        edges = await store.get_graph_edges(v2.id, EdgeType.SUPERSEDES)
        assert len(edges) == 1
        assert edges[0].retires_entities == []
        assert edges[0].target_id == v1.id
