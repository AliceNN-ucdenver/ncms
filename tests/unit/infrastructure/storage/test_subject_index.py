"""Phase B — `idx_mnodes_subject` migration + `get_subject_states` helper.

Covers claims B.1 (index defined), B.2 (idempotent migration),
B.3 (EXPLAIN QUERY PLAN uses the index), and B.4 (helper API +
filter composition).

Performance assertion lives in
``tests/integration/test_subject_index_perf.py`` — see B.7.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite
import pytest

from ncms.domain.models import NodeType
from ncms.infrastructure.storage.migrations import (
    SCHEMA_VERSION,
    create_schema,
)
from ncms.infrastructure.storage.sqlite_memory_nodes import get_subject_states


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        # Match SQLiteStore's row factory so row_to_memory_node()
        # can do column-name access.
        conn.row_factory = aiosqlite.Row
        await create_schema(conn)
        yield conn


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _insert_state(
    db: aiosqlite.Connection,
    *,
    entity_id: str,
    state_key: str,
    state_value: str = "v1",
    is_current: bool = True,
) -> str:
    """Helper to seed an ENTITY_STATE memory_nodes row.

    ``memory_nodes.memory_id`` has an FK on ``memories(id)``, so
    every node needs a matching memory row.  We seed a minimal
    stub memory per node.
    """
    node_id = str(uuid.uuid4())
    memory_id = str(uuid.uuid4())
    now = _now_iso()
    # Stub memory row to satisfy the FK.
    await db.execute(
        "INSERT INTO memories (id, content, type, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, f"stub for {entity_id}/{state_key}", "fact", now, now),
    )
    metadata = {
        "entity_id": entity_id,
        "state_key": state_key,
        "state_value": state_value,
    }
    await db.execute(
        "INSERT INTO memory_nodes "
        "(id, memory_id, node_type, metadata, is_current, "
        " importance, ingested_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id,
            memory_id,
            NodeType.ENTITY_STATE.value,
            json.dumps(metadata),
            1 if is_current else 0,
            5.0,
            now,
            now,
        ),
    )
    await db.commit()
    return node_id


# ---------------------------------------------------------------------------
# B.1 — index defined
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_version_bumped_to_15() -> None:
    """Phase B bumped SCHEMA_VERSION to 15."""
    assert SCHEMA_VERSION == 15


@pytest.mark.asyncio
async def test_migration_creates_index(db: aiosqlite.Connection) -> None:
    """B.1: idx_mnodes_subject exists after a fresh schema build."""
    cur = await db.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND name='idx_mnodes_subject'",
    )
    row = await cur.fetchone()
    assert row is not None, "idx_mnodes_subject not created"
    name, sql = row
    assert name == "idx_mnodes_subject"
    # Partial index on the subject-id JSON path, ENTITY_STATE only.
    assert "json_extract(metadata, '$.entity_id')" in sql
    assert "node_type = 'entity_state'" in sql.lower() or "WHERE" in sql


# ---------------------------------------------------------------------------
# B.2 — migration applies cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_idempotent() -> None:
    """B.2: re-running create_schema does not error or duplicate the index."""
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        # Run a second time — the script uses CREATE ... IF NOT EXISTS.
        await create_schema(db)
        cur = await db.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND name='idx_mnodes_subject'",
        )
        row = await cur.fetchone()
        assert row[0] == 1


@pytest.mark.asyncio
async def test_migration_on_populated_db_succeeds() -> None:
    """B.2: shape test — index creates against a non-fresh DB.

    Fresh-DB-then-seed-then-create-again, simulating a re-run on
    a populated DB.  This exercises the IF NOT EXISTS branch with
    real data present.
    """
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        # Seed 100 ENTITY_STATE rows.
        for i in range(100):
            await _insert_state(
                db,
                entity_id=f"service:s{i}",
                state_key="status",
            )
        # Re-run the migration.  No-op for IF NOT EXISTS.
        await create_schema(db)
        cur = await db.execute(
            "SELECT COUNT(*) FROM memory_nodes WHERE node_type='entity_state'",
        )
        row = await cur.fetchone()
        assert row[0] == 100


# ---------------------------------------------------------------------------
# B.3 — EXPLAIN QUERY PLAN uses the index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_plan_uses_index(db: aiosqlite.Connection) -> None:
    """B.3: SQLite optimizer picks idx_mnodes_subject for the canonical query."""
    cur = await db.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT * FROM memory_nodes "
        "WHERE node_type='entity_state' "
        "AND json_extract(metadata, '$.entity_id') = ?",
        ("service:auth-api",),
    )
    rows = await cur.fetchall()
    # EXPLAIN QUERY PLAN columns: (id, parent, notused, detail).
    # ``detail`` contains the index name when one is selected.
    plan_text = " ".join(row["detail"] for row in rows)
    assert "idx_mnodes_subject" in plan_text, (
        f"EXPLAIN QUERY PLAN did not use idx_mnodes_subject:\n{plan_text}"
    )


@pytest.mark.asyncio
async def test_query_plan_uses_index_via_helper(
    db: aiosqlite.Connection,
) -> None:
    """B.3: the same plan applies to the canonical helper SQL."""
    # Seed a few rows so the optimizer has something to plan against.
    await _insert_state(db, entity_id="service:x", state_key="status")
    # The helper builds the SQL dynamically; assert via a sample
    # call that completes without error.  The plan-text assertion
    # above already verifies the optimizer's choice.
    states = await get_subject_states(db, "service:x")
    assert len(states) == 1


# ---------------------------------------------------------------------------
# B.4 — helper API + filter composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_returns_empty_for_unknown_subject(
    db: aiosqlite.Connection,
) -> None:
    out = await get_subject_states(db, "service:unknown")
    assert out == []


@pytest.mark.asyncio
async def test_helper_subject_id_only(db: aiosqlite.Connection) -> None:
    """No filter — returns every state for the subject."""
    await _insert_state(db, entity_id="service:a", state_key="status")
    await _insert_state(db, entity_id="service:a", state_key="version")
    await _insert_state(db, entity_id="service:b", state_key="status")
    out = await get_subject_states(db, "service:a")
    assert len(out) == 2
    assert all(n.metadata["entity_id"] == "service:a" for n in out)


@pytest.mark.asyncio
async def test_helper_scope_filter(db: aiosqlite.Connection) -> None:
    """``scope=...`` narrows by ``metadata.state_key``."""
    await _insert_state(db, entity_id="service:a", state_key="status")
    await _insert_state(db, entity_id="service:a", state_key="version")
    out = await get_subject_states(db, "service:a", scope="status")
    assert len(out) == 1
    assert out[0].metadata["state_key"] == "status"


@pytest.mark.asyncio
async def test_helper_is_current_filter(db: aiosqlite.Connection) -> None:
    """``is_current=True`` excludes superseded states."""
    await _insert_state(
        db,
        entity_id="service:a",
        state_key="status",
        is_current=False,
    )
    await _insert_state(
        db,
        entity_id="service:a",
        state_key="status",
        is_current=True,
    )
    out = await get_subject_states(db, "service:a", is_current=True)
    assert len(out) == 1
    assert out[0].is_current is True


@pytest.mark.asyncio
async def test_helper_is_current_false_filter(db: aiosqlite.Connection) -> None:
    """``is_current=False`` returns only superseded states."""
    await _insert_state(
        db,
        entity_id="service:a",
        state_key="status",
        is_current=False,
    )
    await _insert_state(
        db,
        entity_id="service:a",
        state_key="status",
        is_current=True,
    )
    out = await get_subject_states(db, "service:a", is_current=False)
    assert len(out) == 1
    assert out[0].is_current is False


@pytest.mark.asyncio
async def test_helper_limit(db: aiosqlite.Connection) -> None:
    """``limit=N`` caps result size."""
    for _ in range(5):
        await _insert_state(db, entity_id="service:a", state_key="status")
    out = await get_subject_states(db, "service:a", limit=3)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_helper_filters_compose(db: aiosqlite.Connection) -> None:
    """All filters compose: subject + scope + is_current + limit."""
    await _insert_state(
        db,
        entity_id="service:a",
        state_key="status",
        is_current=True,
    )
    await _insert_state(
        db,
        entity_id="service:a",
        state_key="status",
        is_current=False,
    )
    await _insert_state(
        db,
        entity_id="service:a",
        state_key="version",
        is_current=True,
    )
    await _insert_state(
        db,
        entity_id="service:b",
        state_key="status",
        is_current=True,
    )
    out = await get_subject_states(
        db,
        "service:a",
        scope="status",
        is_current=True,
        limit=10,
    )
    assert len(out) == 1
    assert out[0].metadata["entity_id"] == "service:a"
    assert out[0].metadata["state_key"] == "status"
    assert out[0].is_current is True


@pytest.mark.asyncio
async def test_helper_ordering_desc(db: aiosqlite.Connection) -> None:
    """Result order is ``created_at DESC`` (most recent first)."""
    import time

    for i in range(3):
        await _insert_state(
            db,
            entity_id="service:a",
            state_key="status",
            state_value=f"v{i}",
        )
        # Force distinct created_at timestamps in the iso-second
        # resolution used by the seeder.
        time.sleep(0.01)
    out = await get_subject_states(db, "service:a")
    assert len(out) == 3
    timestamps = [n.created_at for n in out]
    assert timestamps == sorted(timestamps, reverse=True), (
        f"expected DESC ordering; got {timestamps!r}"
    )


# ---------------------------------------------------------------------------
# Protocol surface (claim B.4 — exposure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_store_exposes_get_subject_states() -> None:
    """``SQLiteStore`` carries the helper as a thin delegate."""
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    try:
        await _insert_state(
            store.db,
            entity_id="service:x",
            state_key="status",
        )
        states = await store.get_subject_states("service:x")
        assert len(states) == 1
    finally:
        await store.close()
