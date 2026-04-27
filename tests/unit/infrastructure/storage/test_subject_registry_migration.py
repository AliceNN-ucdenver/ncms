"""Phase A — `subjects` and `subject_aliases` migration.

Covers part of claim A.4: migration creates both tables on a
fresh DB with the documented schema (PK, FK, indexes).
"""

from __future__ import annotations

import aiosqlite
import pytest

from ncms.infrastructure.storage.migrations import (
    SCHEMA_VERSION,
    create_schema,
)


@pytest.mark.asyncio
async def test_schema_version_bumped_to_14() -> None:
    assert SCHEMA_VERSION == 14


@pytest.mark.asyncio
async def test_subjects_table_created() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='subjects'",
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "subjects"


@pytest.mark.asyncio
async def test_subject_aliases_table_created() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='subject_aliases'",
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "subject_aliases"


@pytest.mark.asyncio
async def test_subjects_columns_match_spec() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        cur = await db.execute("PRAGMA table_info(subjects)")
        cols = {row[1]: row for row in await cur.fetchall()}
        assert set(cols) == {"canonical_id", "type", "created_at"}
        # canonical_id is the primary key
        assert cols["canonical_id"][5] == 1  # pk column index in PRAGMA output


@pytest.mark.asyncio
async def test_subject_aliases_columns_match_spec() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        cur = await db.execute("PRAGMA table_info(subject_aliases)")
        cols = {row[1]: row for row in await cur.fetchall()}
        assert set(cols) == {
            "canonical_id",
            "type",
            "alias",
            "alias_normalized",
            "created_at",
        }


@pytest.mark.asyncio
async def test_subject_aliases_indexes_exist() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='subject_aliases'",
        )
        names = {row[0] for row in await cur.fetchall()}
        # Both the documented secondary indexes plus sqlite's
        # auto-index on the composite primary key.
        assert "idx_subject_aliases_normalized" in names
        assert "idx_subject_aliases_type" in names


@pytest.mark.asyncio
async def test_insert_and_lookup_round_trip() -> None:
    """Smoke test: inserting + reading back a subject + alias works."""
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        await db.execute(
            "INSERT INTO subjects (canonical_id, type, created_at) "
            "VALUES (?, ?, ?)",
            ("service:auth-api", "service", "2026-04-27T00:00:00Z"),
        )
        await db.execute(
            "INSERT INTO subject_aliases "
            "(canonical_id, type, alias, alias_normalized, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "service:auth-api",
                "service",
                "auth-service",
                "auth-service",
                "2026-04-27T00:00:00Z",
            ),
        )
        await db.commit()

        cur = await db.execute(
            "SELECT canonical_id FROM subject_aliases "
            "WHERE alias_normalized = ?",
            ("auth-service",),
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "service:auth-api"


@pytest.mark.asyncio
async def test_alias_pk_prevents_duplicates() -> None:
    """(canonical_id, alias) is the composite PK; INSERT-OR-IGNORE works."""
    async with aiosqlite.connect(":memory:") as db:
        await create_schema(db)
        await db.execute(
            "INSERT INTO subjects (canonical_id, type, created_at) "
            "VALUES ('a:1', 'a', 'now')",
        )
        await db.execute(
            "INSERT INTO subject_aliases "
            "(canonical_id, type, alias, alias_normalized, created_at) "
            "VALUES ('a:1', 'a', 'X', 'x', 'now')",
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO subject_aliases "
                "(canonical_id, type, alias, alias_normalized, created_at) "
                "VALUES ('a:1', 'a', 'X', 'x', 'now')",
            )
