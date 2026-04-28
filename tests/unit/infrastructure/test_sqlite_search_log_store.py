"""Tests for SQLite search log and association-strength persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from ncms.domain.models import SearchLogEntry
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def search_store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


# ── V4 Migration ─────────────────────────────────────────────────────


class TestV4Migration:
    async def test_search_log_table_exists(self, search_store: SQLiteStore) -> None:
        cursor = await search_store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='search_log'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_association_strengths_table_exists(
        self,
        search_store: SQLiteStore,
    ) -> None:
        cursor = await search_store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='association_strengths'"
        )
        row = await cursor.fetchone()
        assert row is not None


# ── Search Log CRUD ──────────────────────────────────────────────────


class TestSearchLog:
    async def test_log_and_retrieve_search(self, search_store: SQLiteStore) -> None:
        entry = SearchLogEntry(
            query="test query",
            query_entities=["entity_a", "entity_b"],
            returned_ids=["mem-1", "mem-2", "mem-3"],
            agent_id="test-agent",
        )
        await search_store.log_search(entry)

        results = await search_store.get_recent_searches(limit=10)
        assert len(results) == 1
        assert results[0].query == "test query"
        assert results[0].query_entities == ["entity_a", "entity_b"]
        assert results[0].returned_ids == ["mem-1", "mem-2", "mem-3"]
        assert results[0].agent_id == "test-agent"

    async def test_get_recent_searches_with_since(
        self,
        search_store: SQLiteStore,
    ) -> None:
        old = SearchLogEntry(
            query="old query",
            timestamp=datetime(2020, 1, 1, tzinfo=UTC),
        )
        new = SearchLogEntry(
            query="new query",
            timestamp=datetime(2025, 6, 1, tzinfo=UTC),
        )
        await search_store.log_search(old)
        await search_store.log_search(new)

        results = await search_store.get_recent_searches(
            since="2024-01-01T00:00:00+00:00",
        )
        assert len(results) == 1
        assert results[0].query == "new query"

    async def test_get_search_access_pairs(self, search_store: SQLiteStore) -> None:
        for i in range(3):
            await search_store.log_search(
                SearchLogEntry(
                    query=f"query {i}",
                    returned_ids=[f"mem-{i}-a", f"mem-{i}-b"],
                )
            )

        pairs = await search_store.get_search_access_pairs()
        assert len(pairs) == 3
        # Each pair is (query, returned_ids)
        assert pairs[0][0] == "query 0"
        assert pairs[0][1] == ["mem-0-a", "mem-0-b"]

    async def test_search_log_limit(self, search_store: SQLiteStore) -> None:
        for i in range(10):
            await search_store.log_search(SearchLogEntry(query=f"q{i}"))
        results = await search_store.get_recent_searches(limit=3)
        assert len(results) == 3


# ── Association Strengths CRUD ───────────────────────────────────────


class TestAssociationStrengths:
    async def test_save_and_load_strengths(self, search_store: SQLiteStore) -> None:
        await search_store.save_association_strength("e1", "e2", 0.75)

        strengths = await search_store.get_association_strengths()
        # Both directions should be present
        assert strengths[("e1", "e2")] == pytest.approx(0.75)
        assert strengths[("e2", "e1")] == pytest.approx(0.75)

    async def test_canonical_ordering(self, search_store: SQLiteStore) -> None:
        """Saving (B, A) then (A, B) should UPSERT the same row."""
        await search_store.save_association_strength("b", "a", 0.3)
        await search_store.save_association_strength("a", "b", 0.9)

        strengths = await search_store.get_association_strengths()
        # Should be the latest value (0.9)
        assert strengths[("a", "b")] == pytest.approx(0.9)
        assert strengths[("b", "a")] == pytest.approx(0.9)

    async def test_multiple_pairs(self, search_store: SQLiteStore) -> None:
        await search_store.save_association_strength("x", "y", 0.5)
        await search_store.save_association_strength("a", "b", 0.8)

        strengths = await search_store.get_association_strengths()
        assert len(strengths) == 4  # 2 pairs × 2 directions
        assert strengths[("x", "y")] == pytest.approx(0.5)
        assert strengths[("a", "b")] == pytest.approx(0.8)

    async def test_empty_strengths(self, search_store: SQLiteStore) -> None:
        strengths = await search_store.get_association_strengths()
        assert strengths == {}
