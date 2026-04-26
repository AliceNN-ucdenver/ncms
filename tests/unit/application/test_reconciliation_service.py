"""Tests for ReconciliationService: 5 relation types × classification + apply actions."""

from __future__ import annotations

import pytest
import pytest_asyncio

from ncms.application.reconciliation_service import ReconciliationService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    EdgeType,
    EntityStateMeta,
    Memory,
    MemoryNode,
    NodeType,
    ReconciliationResult,
    RelationType,
)
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def recon_store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def recon_config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        temporal_enabled=True,
        reconciliation_importance_boost=0.5,
    )


@pytest_asyncio.fixture
async def recon_service(recon_store, recon_config):
    return ReconciliationService(
        store=recon_store,
        config=recon_config,
    )


async def _save_entity_state(
    store: SQLiteStore,
    entity_id: str,
    state_key: str,
    state_value: str,
    *,
    is_current: bool = True,
    state_scope: str | None = None,
    importance: float = 5.0,
) -> MemoryNode:
    """Create a Memory + MemoryNode pair to satisfy FK constraints."""
    mem = Memory(content=f"{entity_id}: {state_key} = {state_value}", domains=["test"])
    await store.save_memory(mem)

    meta: dict = {
        "entity_id": entity_id,
        "state_key": state_key,
        "state_value": state_value,
    }
    if state_scope:
        meta["state_scope"] = state_scope

    node = MemoryNode(
        memory_id=mem.id,
        node_type=NodeType.ENTITY_STATE,
        importance=importance,
        is_current=is_current,
        metadata=meta,
    )
    await store.save_memory_node(node)
    return node


def _make_entity_node(
    entity_id: str,
    state_key: str,
    state_value: str,
    *,
    state_scope: str | None = None,
) -> MemoryNode:
    """Create an in-memory MemoryNode (not persisted) for classification tests."""
    meta: dict = {
        "entity_id": entity_id,
        "state_key": state_key,
        "state_value": state_value,
    }
    if state_scope:
        meta["state_scope"] = state_scope
    return MemoryNode(
        memory_id="mem-dummy",
        node_type=NodeType.ENTITY_STATE,
        metadata=meta,
    )


# ── classify_relation ─────────────────────────────────────────────────


class TestClassifyRelation:
    """Pure classification logic — no store interaction needed."""

    def _classify(
        self,
        new_meta: EntityStateMeta,
        existing_node: MemoryNode,
        existing_meta: EntityStateMeta,
    ) -> ReconciliationResult:
        svc = ReconciliationService(store=object(), config=NCMSConfig(db_path=":memory:"))
        return svc.classify_relation(new_meta, existing_node, existing_meta)

    def test_supports_same_value_same_scope(self) -> None:
        """Same entity+key, same value, same scope → SUPPORTS."""
        existing_node = _make_entity_node("svc-A", "status", "running")
        new_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
        )
        existing_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.SUPPORTS
        assert result.confidence == 0.9

    def test_supports_case_insensitive(self) -> None:
        """Value comparison is case-insensitive."""
        existing_node = _make_entity_node("svc-A", "status", "Running")
        new_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
        )
        existing_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="Running",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.SUPPORTS

    def test_supports_whitespace_trimmed(self) -> None:
        """Values are stripped before comparison."""
        existing_node = _make_entity_node("svc-A", "status", " deployed ")
        new_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="deployed",
        )
        existing_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value=" deployed ",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.SUPPORTS

    def test_refines_same_value_different_scope(self) -> None:
        """Same entity+key, same value, different scope → REFINES."""
        existing_node = _make_entity_node(
            "svc-A",
            "status",
            "running",
            state_scope="global",
        )
        new_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
            state_scope="us-east-1",
        )
        existing_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
            state_scope="global",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.REFINES
        assert result.confidence == 0.8

    def test_supersedes_different_value_same_scope(self) -> None:
        """Same entity+key, different value, same/no scope → SUPERSEDES."""
        existing_node = _make_entity_node("svc-A", "status", "running")
        new_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="stopped",
        )
        existing_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.SUPERSEDES
        assert result.confidence == 0.9
        assert "running" in result.reason
        assert "stopped" in result.reason

    def test_supersedes_no_scope(self) -> None:
        """No scope on either side → SUPERSEDES when values differ."""
        existing_node = _make_entity_node("db-1", "version", "3.2")
        new_meta = EntityStateMeta(
            entity_id="db-1",
            state_key="version",
            state_value="3.3",
        )
        existing_meta = EntityStateMeta(
            entity_id="db-1",
            state_key="version",
            state_value="3.2",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.SUPERSEDES

    def test_conflicts_different_value_different_scope(self) -> None:
        """Same entity+key, different value, different scope → CONFLICTS."""
        existing_node = _make_entity_node(
            "svc-A",
            "status",
            "running",
            state_scope="us-east-1",
        )
        new_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="stopped",
            state_scope="eu-west-1",
        )
        existing_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
            state_scope="us-east-1",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.CONFLICTS
        assert result.confidence == 0.7

    def test_unrelated_different_entity(self) -> None:
        """Different entity → UNRELATED."""
        existing_node = _make_entity_node("svc-B", "status", "running")
        new_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
        )
        existing_meta = EntityStateMeta(
            entity_id="svc-B",
            state_key="status",
            state_value="running",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.UNRELATED

    def test_unrelated_different_key(self) -> None:
        """Same entity, different key → UNRELATED."""
        existing_node = _make_entity_node("svc-A", "version", "2.0")
        new_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="status",
            state_value="running",
        )
        existing_meta = EntityStateMeta(
            entity_id="svc-A",
            state_key="version",
            state_value="2.0",
        )
        result = self._classify(new_meta, existing_node, existing_meta)
        assert result.relation == RelationType.UNRELATED


# ── Full reconcile() pipeline ─────────────────────────────────────────


class TestReconcilePipeline:
    async def test_supports_creates_edge_and_boosts_importance(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        existing = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
        )
        new_node = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
        )

        results = await recon_service.reconcile(new_node)
        assert len(results) == 1
        assert results[0].relation == RelationType.SUPPORTS

        # Check importance boost
        updated_existing = await recon_store.get_memory_node(existing.id)
        assert updated_existing is not None
        assert updated_existing.importance == 5.5  # 5.0 + 0.5 boost

        updated_new = await recon_store.get_memory_node(new_node.id)
        assert updated_new is not None
        assert updated_new.importance == 5.5

        # Check edge created
        edges = await recon_store.get_graph_edges(new_node.id, EdgeType.SUPPORTS)
        assert len(edges) == 1
        assert edges[0].target_id == existing.id

    async def test_supersedes_flips_is_current_and_creates_edges(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        existing = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
        )
        new_node = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "stopped",
        )

        results = await recon_service.reconcile(new_node)
        assert len(results) == 1
        assert results[0].relation == RelationType.SUPERSEDES

        # Existing should no longer be current
        updated_existing = await recon_store.get_memory_node(existing.id)
        assert updated_existing is not None
        assert updated_existing.is_current is False
        assert updated_existing.valid_to is not None
        assert updated_existing.metadata.get("superseded_by") == new_node.id

        # New should be current
        updated_new = await recon_store.get_memory_node(new_node.id)
        assert updated_new is not None
        assert updated_new.is_current is True
        assert updated_new.metadata.get("supersedes") == existing.id

        # Check bidirectional edges
        supersedes_edges = await recon_store.get_graph_edges(
            new_node.id,
            EdgeType.SUPERSEDES,
        )
        assert len(supersedes_edges) == 1
        assert supersedes_edges[0].target_id == existing.id

        superseded_by_edges = await recon_store.get_graph_edges(
            existing.id,
            EdgeType.SUPERSEDED_BY,
        )
        assert len(superseded_by_edges) == 1
        assert superseded_by_edges[0].target_id == new_node.id

    async def test_refines_creates_edge(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
            state_scope="global",
        )
        new_node = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
            state_scope="us-east-1",
        )

        results = await recon_service.reconcile(new_node)
        assert len(results) == 1
        assert results[0].relation == RelationType.REFINES

        edges = await recon_store.get_graph_edges(new_node.id, EdgeType.REFINES)
        assert len(edges) == 1

    async def test_conflicts_creates_bidirectional_edges(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        existing = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
            state_scope="us-east-1",
        )
        new_node = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "stopped",
            state_scope="eu-west-1",
        )

        results = await recon_service.reconcile(new_node)
        assert len(results) == 1
        assert results[0].relation == RelationType.CONFLICTS

        # Both directions
        edges_new = await recon_store.get_graph_edges(
            new_node.id,
            EdgeType.CONFLICTS_WITH,
        )
        edges_existing = await recon_store.get_graph_edges(
            existing.id,
            EdgeType.CONFLICTS_WITH,
        )
        assert len(edges_new) == 1
        assert len(edges_existing) == 1
        assert edges_new[0].metadata.get("flagged_for_review") is True

    async def test_skips_self_comparison(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        """reconcile() should not compare the new node to itself."""
        node = await _save_entity_state(recon_store, "svc-A", "status", "running")

        results = await recon_service.reconcile(node)
        assert len(results) == 0

    async def test_returns_empty_for_non_entity_node(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        """Nodes without entity state metadata return empty results."""
        mem = Memory(content="Just a note", domains=["test"])
        await recon_store.save_memory(mem)
        node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.ATOMIC,
            metadata={},
        )
        results = await recon_service.reconcile(node)
        assert results == []

    async def test_supersession_chain(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        """v1 → v2 → v3: only v3 should be current at the end."""
        await _save_entity_state(recon_store, "svc-A", "status", "starting")

        v2 = await _save_entity_state(recon_store, "svc-A", "status", "running")
        await recon_service.reconcile(v2)

        v3 = await _save_entity_state(recon_store, "svc-A", "status", "stopped")
        await recon_service.reconcile(v3)

        # Only v3 should be current
        current = await recon_store.get_current_entity_states("svc-A", "status")
        assert len(current) == 1
        assert current[0].id == v3.id

        # v1 and v2 should be superseded
        all_states = await recon_store.get_entity_states_by_entity("svc-A")
        superseded = [n for n in all_states if not n.is_current]
        assert len(superseded) == 2

    async def test_importance_boost_capped_at_10(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        """Importance boost should not exceed 10.0."""
        existing = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
            importance=9.8,
        )
        new_node = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
            importance=9.9,
        )

        await recon_service.reconcile(new_node)

        updated = await recon_store.get_memory_node(existing.id)
        assert updated is not None
        assert updated.importance == 10.0  # Capped, not 10.3

    async def test_multiple_existing_states(
        self,
        recon_store: SQLiteStore,
        recon_service: ReconciliationService,
    ) -> None:
        """When multiple current states exist, reconcile compares against each."""
        await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
            state_scope="us-east-1",
        )
        await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
            state_scope="eu-west-1",
        )

        new_node = await _save_entity_state(
            recon_store,
            "svc-A",
            "status",
            "running",
            state_scope="ap-south-1",
        )

        results = await recon_service.reconcile(new_node)
        # Should produce 2 results (one per existing)
        assert len(results) == 2
        # Both should be REFINES (same value, different scope)
        assert all(r.relation == RelationType.REFINES for r in results)


# ── extract_entity_state_meta ─────────────────────────────────────────


class TestExtractEntityStateMeta:
    """Test the static helper for extracting entity state metadata."""

    @staticmethod
    def _extract(content: str, entities: list[dict] | None = None) -> dict:
        from ncms.application.ingestion import IngestionPipeline

        return IngestionPipeline.extract_entity_state_meta(
            content,
            entities or [],
        )

    def test_colon_equals_pattern(self) -> None:
        result = self._extract("auth-service: status = deployed")
        assert result["entity_id"] == "auth-service"
        assert result["state_key"] == "status"
        assert result["state_value"] == "deployed"

    def test_is_pattern(self) -> None:
        result = self._extract("auth-service status is deployed")
        assert result["entity_id"] == "auth-service"
        assert result["state_key"] == "status"
        assert result["state_value"] == "deployed"

    def test_changed_to_pattern(self) -> None:
        result = self._extract("db-1 version changed to 3.3")
        assert result["entity_id"] == "db-1"
        assert result["state_key"] == "version"
        assert result["state_value"] == "3.3"

    def test_fallback_to_first_entity(self) -> None:
        entities = [{"name": "auth-service", "type": "service"}]
        result = self._extract(
            "Deployed new release to production with zero downtime",
            entities,
        )
        assert result["entity_id"] == "auth-service"
        assert result["state_key"] == "state"
        assert "Deployed" in result["state_value"]

    def test_empty_when_no_pattern_no_entities(self) -> None:
        result = self._extract("Just some random content")
        assert result == {}
