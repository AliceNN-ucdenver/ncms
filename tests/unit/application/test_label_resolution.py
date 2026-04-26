"""Unit tests for ``load_cached_labels`` in ``application/label_cache.py``."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from ncms.application.label_cache import load_cached_labels
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store() -> SQLiteStore:
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


class TestLoadCachedLabels:
    async def test_empty_store_returns_empty(self, store: SQLiteStore) -> None:
        """No cached labels should return empty dict."""
        result = await load_cached_labels(store, ["api"])
        assert result == {}

    async def test_cached_labels_loaded(self, store: SQLiteStore) -> None:
        """Pre-populated labels should be returned."""
        labels = ["endpoint", "service", "protocol"]
        await store.set_consolidation_value(
            "entity_labels:api",
            json.dumps(labels),
        )
        result = await load_cached_labels(store, ["api"])
        assert result == {"api": labels}

    async def test_multiple_domains(self, store: SQLiteStore) -> None:
        """Should load labels for all requested domains."""
        await store.set_consolidation_value(
            "entity_labels:api",
            json.dumps(["endpoint", "service"]),
        )
        await store.set_consolidation_value(
            "entity_labels:db",
            json.dumps(["table", "column"]),
        )
        result = await load_cached_labels(store, ["api", "db"])
        assert result == {
            "api": ["endpoint", "service"],
            "db": ["table", "column"],
        }

    async def test_missing_domain_excluded(
        self,
        store: SQLiteStore,
    ) -> None:
        """Domains without cached labels should not appear in result."""
        await store.set_consolidation_value(
            "entity_labels:api",
            json.dumps(["endpoint"]),
        )
        result = await load_cached_labels(store, ["api", "finance"])
        assert "api" in result
        assert "finance" not in result

    async def test_invalid_json_ignored(self, store: SQLiteStore) -> None:
        """Corrupted cache entries should be silently ignored."""
        await store.set_consolidation_value(
            "entity_labels:api",
            "not-valid-json",
        )
        result = await load_cached_labels(store, ["api"])
        assert result == {}

    async def test_non_list_json_ignored(self, store: SQLiteStore) -> None:
        """Cache entries that aren't JSON arrays should be ignored."""
        await store.set_consolidation_value(
            "entity_labels:api",
            json.dumps({"not": "a list"}),
        )
        result = await load_cached_labels(store, ["api"])
        assert result == {}

    async def test_empty_domains_returns_empty(
        self,
        store: SQLiteStore,
    ) -> None:
        """Empty domain list should return empty dict."""
        result = await load_cached_labels(store, [])
        assert result == {}

    async def test_keep_universal_loaded(self, store: SQLiteStore) -> None:
        """The ``_keep_universal`` flag is decoded and merged in."""
        await store.set_consolidation_value(
            "_keep_universal",
            json.dumps(True),
        )
        result = await load_cached_labels(store, [])
        assert result.get("_keep_universal") is True

    @pytest.mark.parametrize("bad_value", ["null", "{}"])
    async def test_keep_universal_bad_values(
        self,
        store: SQLiteStore,
        bad_value: str,
    ) -> None:
        """Non-fatal decode: bad ``_keep_universal`` values are kept as parsed.

        The helper is read-only and tolerant — json.loads('null') is None,
        json.loads('{}') is an empty dict; both are returned as-is rather
        than raising.
        """
        await store.set_consolidation_value("_keep_universal", bad_value)
        result = await load_cached_labels(store, [])
        assert "_keep_universal" in result
