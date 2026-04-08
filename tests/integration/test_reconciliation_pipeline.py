"""Integration test: full store → admit → route → reconcile → supersede → temporal queries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from ncms.application.reconciliation_service import ReconciliationService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    EdgeType,
    Memory,
    MemoryNode,
    NodeType,
    RelationType,
)
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        reconciliation_enabled=True,
        reconciliation_importance_boost=0.5,
    )


@pytest_asyncio.fixture
async def service(store, config):
    return ReconciliationService(store=store, config=config)


async def _create_entity_state(
    store: SQLiteStore,
    entity_id: str,
    state_key: str,
    state_value: str,
    *,
    state_scope: str | None = None,
    observed_at: datetime | None = None,
) -> MemoryNode:
    """Create a Memory + entity state MemoryNode pair."""
    mem = Memory(
        content=f"{entity_id}: {state_key} = {state_value}",
        domains=["integration-test"],
    )
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
        importance=5.0,
        metadata=meta,
        observed_at=observed_at,
    )
    await store.save_memory_node(node)
    return node


class TestFullReconciliationPipeline:
    """End-to-end: create states, reconcile, verify supersession and temporal queries."""

    async def test_three_state_supersession_chain(
        self,
        store: SQLiteStore,
        service: ReconciliationService,
    ) -> None:
        """v1(starting) → v2(running) → v3(stopped): full supersession chain."""
        v1 = await _create_entity_state(store, "auth-svc", "status", "starting")

        v2 = await _create_entity_state(store, "auth-svc", "status", "running")
        results = await service.reconcile(v2)
        assert len(results) == 1
        assert results[0].relation == RelationType.SUPERSEDES

        v3 = await _create_entity_state(store, "auth-svc", "status", "stopped")
        results = await service.reconcile(v3)
        assert len(results) == 1
        assert results[0].relation == RelationType.SUPERSEDES

        # Verify current state
        current = await store.get_current_state("auth-svc", "status")
        assert current is not None
        assert current.id == v3.id
        assert current.metadata["state_value"] == "stopped"

        # Verify history
        history = await store.get_state_history("auth-svc", "status")
        assert len(history) == 3
        values = [n.metadata["state_value"] for n in history]
        assert values == ["starting", "running", "stopped"]

        # Verify supersession chain edges
        v3_edges = await store.get_graph_edges(v3.id, EdgeType.SUPERSEDES)
        assert len(v3_edges) == 1
        assert v3_edges[0].target_id == v2.id

        v2_edges = await store.get_graph_edges(v2.id, EdgeType.SUPERSEDES)
        assert len(v2_edges) == 1
        assert v2_edges[0].target_id == v1.id

    async def test_parallel_scoped_states_conflict(
        self,
        store: SQLiteStore,
        service: ReconciliationService,
    ) -> None:
        """Different values in different scopes → CONFLICTS (parallel truths)."""
        await _create_entity_state(
            store, "api-gw", "status", "running", state_scope="us-east-1",
        )
        eu = await _create_entity_state(
            store, "api-gw", "status", "degraded", state_scope="eu-west-1",
        )

        results = await service.reconcile(eu)
        assert len(results) == 1
        assert results[0].relation == RelationType.CONFLICTS

        # Both should still be current (conflicts don't supersede)
        current_states = await store.get_current_entity_states("api-gw", "status")
        assert len(current_states) == 2

    async def test_support_then_supersede(
        self,
        store: SQLiteStore,
        service: ReconciliationService,
    ) -> None:
        """Same value → SUPPORTS, then different value → SUPERSEDES."""
        v1 = await _create_entity_state(store, "db-main", "version", "3.2")

        # Second report of same value → supports
        v2 = await _create_entity_state(store, "db-main", "version", "3.2")
        results = await service.reconcile(v2)
        assert any(r.relation == RelationType.SUPPORTS for r in results)

        # Importance should be boosted
        v1_updated = await store.get_memory_node(v1.id)
        assert v1_updated is not None
        assert v1_updated.importance > 5.0

        # New version → supersedes both current states
        v3 = await _create_entity_state(store, "db-main", "version", "3.3")
        results = await service.reconcile(v3)
        assert len(results) == 2  # Two current states to compare against
        assert all(r.relation == RelationType.SUPERSEDES for r in results)

        current = await store.get_current_state("db-main", "version")
        assert current is not None
        assert current.id == v3.id

    async def test_bitemporal_observed_at_persists(
        self,
        store: SQLiteStore,
        service: ReconciliationService,
    ) -> None:
        """Verify observed_at is stored and retrievable through temporal queries."""
        observed = datetime(2026, 3, 10, 14, 30, 0, tzinfo=UTC)
        node = await _create_entity_state(
            store, "svc-X", "status", "deployed", observed_at=observed,
        )

        fetched = await store.get_memory_node(node.id)
        assert fetched is not None
        assert fetched.observed_at == observed
        assert fetched.ingested_at is not None

    async def test_state_changes_since(
        self,
        store: SQLiteStore,
        service: ReconciliationService,
    ) -> None:
        """get_state_changes_since returns all entity state changes after a timestamp."""
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

        await _create_entity_state(store, "svc-1", "status", "up")
        await _create_entity_state(store, "svc-2", "status", "down")
        await _create_entity_state(store, "svc-1", "version", "1.0")

        changes = await store.get_state_changes_since(past)
        assert len(changes) == 3

    async def test_multiple_entities_independent(
        self,
        store: SQLiteStore,
        service: ReconciliationService,
    ) -> None:
        """Reconciliation for one entity doesn't affect another."""
        await _create_entity_state(store, "svc-A", "status", "running")
        b1 = await _create_entity_state(store, "svc-B", "status", "running")

        a2 = await _create_entity_state(store, "svc-A", "status", "stopped")
        results = await service.reconcile(a2)
        assert len(results) == 1
        assert results[0].relation == RelationType.SUPERSEDES

        # svc-B should be unaffected
        b_current = await store.get_current_state("svc-B", "status")
        assert b_current is not None
        assert b_current.id == b1.id
        assert b_current.is_current is True
