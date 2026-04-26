"""Unit tests for ``SQLiteStore`` content-range methods.

P1-temporal-experiment schema: `memory_content_ranges` stores a
per-memory ISO-8601 interval derived from GLiNER-extracted temporal
spans.  These tests exercise the round-trip save/get/batch API.
"""

from __future__ import annotations

import pytest_asyncio

from ncms.domain.models import Memory
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


async def _seed_memory(store: SQLiteStore, memory_id: str) -> None:
    """Insert a minimal parent memory so FK constraints pass."""
    mem = Memory(content=f"seed {memory_id}")
    mem.id = memory_id
    await store.save_memory(mem)


@pytest_asyncio.fixture
async def store() -> SQLiteStore:
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


class TestContentRangeRoundTrip:
    async def test_save_and_get_single(self, store: SQLiteStore) -> None:
        await _seed_memory(store, "m1")
        await store.save_content_range(
            memory_id="m1",
            range_start="2024-06-05T00:00:00+00:00",
            range_end="2024-06-06T00:00:00+00:00",
            span_count=1,
            source="gliner",
        )
        got = await store.get_content_range("m1")
        assert got == (
            "2024-06-05T00:00:00+00:00",
            "2024-06-06T00:00:00+00:00",
        )

    async def test_get_missing_returns_none(self, store: SQLiteStore) -> None:
        got = await store.get_content_range("nope")
        assert got is None

    async def test_save_is_upsert(self, store: SQLiteStore) -> None:
        """Re-ingesting a memory's range replaces, doesn't duplicate."""
        await _seed_memory(store, "m1")
        await store.save_content_range(
            "m1",
            "2024-01-01T00:00:00+00:00",
            "2024-01-02T00:00:00+00:00",
            1,
            "gliner",
        )
        await store.save_content_range(
            "m1",
            "2025-06-01T00:00:00+00:00",
            "2025-06-30T00:00:00+00:00",
            5,
            "gliner",
        )
        got = await store.get_content_range("m1")
        assert got == (
            "2025-06-01T00:00:00+00:00",
            "2025-06-30T00:00:00+00:00",
        )

    async def test_batch_lookup(self, store: SQLiteStore) -> None:
        await _seed_memory(store, "a")
        await _seed_memory(store, "b")
        await store.save_content_range(
            "a",
            "2024-01-01T00:00:00+00:00",
            "2024-01-02T00:00:00+00:00",
            1,
            "gliner",
        )
        await store.save_content_range(
            "b",
            "2024-02-01T00:00:00+00:00",
            "2024-03-01T00:00:00+00:00",
            2,
            "gliner",
        )
        got = await store.get_content_ranges_batch(["a", "b", "missing"])
        assert set(got.keys()) == {"a", "b"}
        assert got["a"] == (
            "2024-01-01T00:00:00+00:00",
            "2024-01-02T00:00:00+00:00",
        )
        assert got["b"] == (
            "2024-02-01T00:00:00+00:00",
            "2024-03-01T00:00:00+00:00",
        )

    async def test_batch_empty_input(self, store: SQLiteStore) -> None:
        got = await store.get_content_ranges_batch([])
        assert got == {}

    async def test_source_field_persists(self, store: SQLiteStore) -> None:
        """`source` is a debug-facing field but must round-trip for
        later observability work."""
        await _seed_memory(store, "m1")
        await store.save_content_range(
            "m1",
            "2024-01-01T00:00:00+00:00",
            "2024-01-02T00:00:00+00:00",
            1,
            "mixed",
        )
        # Get back via raw SQL since the public API surfaces only the range
        cursor = await store.db.execute(
            "SELECT source, span_count FROM memory_content_ranges WHERE memory_id = ?",
            ("m1",),
        )
        row = await cursor.fetchone()
        assert row[0] == "mixed"
        assert row[1] == 1
