"""Tests for Phase 2 SQLite store: entity state queries, V3 migration, temporal queries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from ncms.domain.models import Memory, MemoryNode, NodeType
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def phase2_store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


async def _save_entity_state(
    store: SQLiteStore,
    entity_id: str,
    state_key: str,
    state_value: str,
    *,
    memory_id: str | None = None,
    is_current: bool = True,
    state_scope: str | None = None,
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
        importance=5.0,
        is_current=is_current,
        metadata=meta,
    )
    await store.save_memory_node(node)
    return node


# ── get_current_entity_states ─────────────────────────────────────────


class TestGetCurrentEntityStates:
    async def test_returns_matching_current_states(self, phase2_store: SQLiteStore) -> None:
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")

        results = await phase2_store.get_current_entity_states("svc-A", "status")
        assert len(results) == 1
        assert results[0].id == node.id

    async def test_excludes_superseded_states(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        await _save_entity_state(
            phase2_store,
            "svc-A",
            "status",
            "stopped",
            is_current=False,
        )

        results = await phase2_store.get_current_entity_states("svc-A", "status")
        assert len(results) == 1
        assert results[0].metadata["state_value"] == "running"

    async def test_filters_by_entity_id(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        await _save_entity_state(phase2_store, "svc-B", "status", "stopped")

        results = await phase2_store.get_current_entity_states("svc-A", "status")
        assert len(results) == 1
        assert results[0].metadata["entity_id"] == "svc-A"

    async def test_filters_by_state_key(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        await _save_entity_state(phase2_store, "svc-A", "version", "2.0")

        results = await phase2_store.get_current_entity_states("svc-A", "status")
        assert len(results) == 1
        assert results[0].metadata["state_key"] == "status"

    async def test_returns_empty_for_no_match(self, phase2_store: SQLiteStore) -> None:
        results = await phase2_store.get_current_entity_states("nonexistent", "status")
        assert results == []

    async def test_multiple_current_states(self, phase2_store: SQLiteStore) -> None:
        """Multiple nodes can be current for same entity+key (e.g. different scopes)."""
        await _save_entity_state(
            phase2_store,
            "svc-A",
            "status",
            "running",
            state_scope="us-east-1",
        )
        await _save_entity_state(
            phase2_store,
            "svc-A",
            "status",
            "running",
            state_scope="eu-west-1",
        )

        results = await phase2_store.get_current_entity_states("svc-A", "status")
        assert len(results) == 2


# ── get_entity_states_by_entity ───────────────────────────────────────


class TestGetEntityStatesByEntity:
    async def test_returns_all_states_for_entity(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        await _save_entity_state(
            phase2_store,
            "svc-A",
            "status",
            "stopped",
            is_current=False,
        )
        await _save_entity_state(phase2_store, "svc-A", "version", "2.0")

        results = await phase2_store.get_entity_states_by_entity("svc-A")
        assert len(results) == 3

    async def test_excludes_other_entities(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        await _save_entity_state(phase2_store, "svc-B", "status", "stopped")

        results = await phase2_store.get_entity_states_by_entity("svc-A")
        assert len(results) == 1
        assert results[0].metadata["entity_id"] == "svc-A"

    async def test_returns_empty_for_no_entity(self, phase2_store: SQLiteStore) -> None:
        results = await phase2_store.get_entity_states_by_entity("ghost")
        assert results == []


# ── update_memory_node ────────────────────────────────────────────────


class TestUpdateMemoryNode:
    async def test_updates_importance(self, phase2_store: SQLiteStore) -> None:
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")

        node.importance = 8.0
        await phase2_store.update_memory_node(node)

        fetched = await phase2_store.get_memory_node(node.id)
        assert fetched is not None
        assert fetched.importance == 8.0

    async def test_updates_is_current_flag(self, phase2_store: SQLiteStore) -> None:
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")

        node.is_current = False
        await phase2_store.update_memory_node(node)

        fetched = await phase2_store.get_memory_node(node.id)
        assert fetched is not None
        assert fetched.is_current is False

    async def test_updates_metadata(self, phase2_store: SQLiteStore) -> None:
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")

        new_meta = dict(node.metadata)
        new_meta["superseded_by"] = "node-99"
        node.metadata = new_meta
        await phase2_store.update_memory_node(node)

        fetched = await phase2_store.get_memory_node(node.id)
        assert fetched is not None
        assert fetched.metadata["superseded_by"] == "node-99"


# ── Schema V3 Migration ──────────────────────────────────────────────


class TestV3Migration:
    async def test_schema_version_at_least_3(self, phase2_store: SQLiteStore) -> None:
        cursor = await phase2_store.db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] >= 3

    async def test_observed_at_column_exists(self, phase2_store: SQLiteStore) -> None:
        cursor = await phase2_store.db.execute("PRAGMA table_info(memory_nodes)")
        cols = {r[1] for r in await cursor.fetchall()}
        assert "observed_at" in cols

    async def test_ingested_at_column_exists(self, phase2_store: SQLiteStore) -> None:
        cursor = await phase2_store.db.execute("PRAGMA table_info(memory_nodes)")
        cols = {r[1] for r in await cursor.fetchall()}
        assert "ingested_at" in cols


# ── Bitemporal Fields Persistence ────────────────────────────────────


class TestBitemporalFields:
    async def test_observed_at_roundtrip(self, phase2_store: SQLiteStore) -> None:
        observed = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")
        node.observed_at = observed
        await phase2_store.update_memory_node(node)

        fetched = await phase2_store.get_memory_node(node.id)
        assert fetched is not None
        assert fetched.observed_at == observed

    async def test_ingested_at_auto_set(self, phase2_store: SQLiteStore) -> None:
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")
        fetched = await phase2_store.get_memory_node(node.id)
        assert fetched is not None
        assert fetched.ingested_at is not None

    async def test_observed_at_nullable(self, phase2_store: SQLiteStore) -> None:
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")
        fetched = await phase2_store.get_memory_node(node.id)
        assert fetched is not None
        assert fetched.observed_at is None  # Not set by default


# ── Temporal Queries (Phase 2B) ──────────────────────────────────────


class TestGetCurrentState:
    async def test_returns_current_state(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        result = await phase2_store.get_current_state("svc-A", "status")
        assert result is not None
        assert result.metadata["state_value"] == "running"

    async def test_returns_none_when_no_state(self, phase2_store: SQLiteStore) -> None:
        result = await phase2_store.get_current_state("ghost", "status")
        assert result is None

    async def test_prefers_most_recent(self, phase2_store: SQLiteStore) -> None:
        """When multiple current states exist, returns the most recent."""
        await _save_entity_state(
            phase2_store,
            "svc-A",
            "status",
            "running",
            state_scope="us-east",
        )
        newer = await _save_entity_state(
            phase2_store,
            "svc-A",
            "status",
            "running",
            state_scope="eu-west",
        )
        result = await phase2_store.get_current_state("svc-A", "status")
        assert result is not None
        assert result.id == newer.id


class TestGetStateAtTime:
    async def test_finds_state_valid_at_time(self, phase2_store: SQLiteStore) -> None:
        now = datetime.now(UTC)
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")
        node.valid_from = now - timedelta(hours=2)
        node.valid_to = now - timedelta(hours=1)
        node.is_current = False
        await phase2_store.update_memory_node(node)

        query_time = (now - timedelta(hours=1, minutes=30)).isoformat()
        result = await phase2_store.get_state_at_time("svc-A", "status", query_time)
        assert result is not None
        assert result.id == node.id

    async def test_returns_none_before_any_state(self, phase2_store: SQLiteStore) -> None:
        far_past = "2000-01-01T00:00:00+00:00"
        result = await phase2_store.get_state_at_time("svc-A", "status", far_past)
        assert result is None

    async def test_fallback_to_created_at(self, phase2_store: SQLiteStore) -> None:
        """When no valid_from is set, falls back to created_at."""
        node = await _save_entity_state(phase2_store, "svc-A", "status", "running")
        # No valid_from set — should still find via created_at fallback
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        result = await phase2_store.get_state_at_time("svc-A", "status", future)
        assert result is not None
        assert result.id == node.id


class TestGetStateChangesSince:
    async def test_returns_recent_changes(self, phase2_store: SQLiteStore) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        await _save_entity_state(phase2_store, "svc-B", "version", "2.0")

        results = await phase2_store.get_state_changes_since(past)
        assert len(results) == 2

    async def test_returns_empty_for_future_timestamp(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        results = await phase2_store.get_state_changes_since(future)
        assert results == []


class TestGetStateHistory:
    async def test_returns_full_history(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "starting")
        await _save_entity_state(
            phase2_store,
            "svc-A",
            "status",
            "running",
            is_current=False,
        )
        await _save_entity_state(phase2_store, "svc-A", "status", "stopped")

        history = await phase2_store.get_state_history("svc-A", "status")
        assert len(history) == 3
        # Chronological order
        values = [n.metadata["state_value"] for n in history]
        assert values == ["starting", "running", "stopped"]

    async def test_excludes_other_keys(self, phase2_store: SQLiteStore) -> None:
        await _save_entity_state(phase2_store, "svc-A", "status", "running")
        await _save_entity_state(phase2_store, "svc-A", "version", "2.0")

        history = await phase2_store.get_state_history("svc-A", "status")
        assert len(history) == 1

    async def test_returns_empty_for_unknown(self, phase2_store: SQLiteStore) -> None:
        history = await phase2_store.get_state_history("ghost", "status")
        assert history == []
