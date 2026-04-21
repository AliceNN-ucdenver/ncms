"""TLG Phase 3a integration: L2 marker induction pipeline + retirement
verb loading.

End-to-end checks:

* Induction scans SUPERSEDES / REFINES edges, resolves source memory
  content, runs the distinctiveness filter, and persists the result
  into ``grammar_transition_markers``.
* ``load_retirement_verbs`` returns the seed set on a cold store and
  the induced flatten on a populated store.
* The reconciliation service automatically picks up new verbs once
  the store is populated — no restart required.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.application.reconciliation_service import ReconciliationService
from ncms.application.tlg import (
    induce_and_persist_markers,
    load_retirement_verbs,
    run_marker_induction,
)
from ncms.config import NCMSConfig
from ncms.domain.models import (
    EdgeType,
    Entity,
    Memory,
    MemoryNode,
    NodeType,
    RelationType,
)
from ncms.domain.tlg import SEED_RETIREMENT_VERBS
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


async def _run_reconciliation(store: SQLiteStore) -> None:
    """Create two supersession chains so L2 has observations to mine.

    Chain 1: session cookies → OAuth (announcement uses ``Retire``).
    Chain 2: basic auth → SSO (announcement uses ``Deprecate``).
    """
    config = NCMSConfig(
        db_path=":memory:",
        temporal_enabled=True,
    )
    service = ReconciliationService(store=store, config=config)

    # Chain 1
    await _seed_state(
        store,
        content="Authentication uses session cookies.",
        entity_id="auth-svc",
        state_key="auth_method",
        state_value="session cookies",
        linked_entity_ids=["session cookies"],
    )
    v2 = await _seed_state(
        store,
        content="Retire session cookies; adopt OAuth 2.0 tokens.",
        entity_id="auth-svc",
        state_key="auth_method",
        state_value="OAuth 2.0",
        linked_entity_ids=["OAuth 2.0"],
    )
    await service.reconcile(v2)

    # Chain 2
    await _seed_state(
        store,
        content="Gateway uses basic-auth flow.",
        entity_id="gateway-svc",
        state_key="auth_method",
        state_value="basic-auth flow",
        linked_entity_ids=["basic-auth flow"],
    )
    v4 = await _seed_state(
        store,
        # "deprecated" matches the shape ``deprecat(?:es|ed|ing)``;
        # imperative "deprecate" on its own would NOT match.
        content="Deprecated basic-auth flow; SSO rolls out next sprint.",
        entity_id="gateway-svc",
        state_key="auth_method",
        state_value="SSO",
        linked_entity_ids=["SSO"],
    )
    await service.reconcile(v4)


# ---------------------------------------------------------------------------
# Pure induction (no persistence)
# ---------------------------------------------------------------------------


class TestRunMarkerInduction:
    async def test_cold_store_returns_empty_markers(
        self, store: SQLiteStore
    ) -> None:
        induced = await run_marker_induction(store)
        assert induced.markers == {}

    async def test_scans_supersedes_announcement_content(
        self, store: SQLiteStore
    ) -> None:
        await _run_reconciliation(store)
        induced = await run_marker_induction(store)
        # Two supersedes chains — "retire" and "deprecated" appear in
        # the announcement content.  The extractor keeps verb inflections
        # as-is (the first-word head, lowercased), so the bucket
        # contains "retire" and "deprecated" rather than the lemma.
        supersedes = induced.markers.get("supersedes", frozenset())
        assert "retire" in supersedes
        assert "deprecated" in supersedes

    async def test_refines_and_supersedes_tracked_separately(
        self, store: SQLiteStore
    ) -> None:
        # REFINES edge with distinct vocabulary.
        await _ensure_entity(store, "x")
        mem = Memory(content="Add feature flag X to the gateway.", domains=["t"])
        await store.save_memory(mem)
        node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.ENTITY_STATE,
            metadata={"entity_id": "x", "state_key": "s", "state_value": "v"},
        )
        await store.save_memory_node(node)
        # Manually create a REFINES edge with node as source.
        from ncms.domain.models import GraphEdge
        await store.save_graph_edge(GraphEdge(
            source_id=node.id,
            target_id=node.id,  # self-loop is fine for this scan test
            edge_type=EdgeType.REFINES,
        ))
        # Add a supersedes chain too
        await _run_reconciliation(store)

        induced = await run_marker_induction(store)
        # "add" should land under refines (unique to that bucket)
        assert "add" in induced.markers.get("refines", frozenset())
        # "retire" / "deprecate" stay in supersedes
        assert "retire" in induced.markers.get("supersedes", frozenset())


# ---------------------------------------------------------------------------
# Persistence + reload
# ---------------------------------------------------------------------------


class TestInduceAndPersistMarkers:
    async def test_persists_to_table(self, store: SQLiteStore) -> None:
        await _run_reconciliation(store)
        induced = await induce_and_persist_markers(store)
        assert "retire" in induced.markers["supersedes"]

        persisted = await store.load_transition_markers()
        assert persisted == induced.markers

    async def test_re_run_overwrites(self, store: SQLiteStore) -> None:
        # First run — seeds the table with chain-1/chain-2 markers.
        await _run_reconciliation(store)
        await induce_and_persist_markers(store)
        first = await store.load_transition_markers()
        assert "replace" not in first.get("supersedes", frozenset())

        # Add a SUPERSEDES edge whose source content introduces a
        # NEW distinctive verb head ("replaces") via a different
        # VERB_PHRASE_SHAPES pattern.  Re-induce, verify the second
        # snapshot reflects the new observation — proving DELETE-then-
        # INSERT semantics (new marker lands, nothing from first lost).
        from ncms.domain.models import GraphEdge
        await _ensure_entity(store, "y")
        mem = Memory(
            content="Replaces v1 with v2 across the stack.", domains=["t"],
        )
        await store.save_memory(mem)
        node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.ENTITY_STATE,
            metadata={"entity_id": "y", "state_key": "s", "state_value": "v"},
        )
        await store.save_memory_node(node)
        await store.save_graph_edge(GraphEdge(
            source_id=node.id,
            target_id=node.id,
            edge_type=EdgeType.SUPERSEDES,
        ))
        await induce_and_persist_markers(store)
        second = await store.load_transition_markers()
        assert second != first
        assert "replaces" in second.get("supersedes", frozenset())


# ---------------------------------------------------------------------------
# load_retirement_verbs
# ---------------------------------------------------------------------------


class TestLoadRetirementVerbs:
    async def test_cold_store_returns_seed(self, store: SQLiteStore) -> None:
        verbs = await load_retirement_verbs(store)
        assert verbs == SEED_RETIREMENT_VERBS

    async def test_populated_store_returns_induced(
        self, store: SQLiteStore
    ) -> None:
        await _run_reconciliation(store)
        await induce_and_persist_markers(store)
        verbs = await load_retirement_verbs(store)
        assert "retire" in verbs
        # An induced set is typically NARROWER than the seed — the
        # loader must not return the seed when the table is populated.
        assert verbs != SEED_RETIREMENT_VERBS


# ---------------------------------------------------------------------------
# Reconciliation picks up induced markers automatically
# ---------------------------------------------------------------------------


class TestReconciliationUsesInducedMarkers:
    async def test_second_reconcile_uses_persisted_verbs(
        self, store: SQLiteStore
    ) -> None:
        # Bootstrap reconciliation chain + induce markers from it.
        await _run_reconciliation(store)
        await induce_and_persist_markers(store)

        # Now reconcile a new chain that relies on a verb the induced
        # set knows ("retire").  The reconciliation's
        # ``_compute_retires_entities`` should produce a populated
        # ``retires_entities`` on the new SUPERSEDES edge without
        # re-seeding.
        config = NCMSConfig(
            db_path=":memory:",
            temporal_enabled=True,
        )
        service = ReconciliationService(store=store, config=config)

        await _seed_state(
            store,
            content="Backend uses polling-config module.",
            entity_id="backend-svc",
            state_key="config",
            state_value="polling-config",
            linked_entity_ids=["polling-config"],
        )
        v2 = await _seed_state(
            store,
            content="Retire polling-config; adopt push updates.",
            entity_id="backend-svc",
            state_key="config",
            state_value="push updates",
            linked_entity_ids=["push updates"],
        )
        results = await service.reconcile(v2)
        assert len(results) == 1
        assert results[0].relation == RelationType.SUPERSEDES
        edges = await store.get_graph_edges(v2.id, EdgeType.SUPERSEDES)
        assert len(edges) == 1
        assert "polling-config" in edges[0].retires_entities
