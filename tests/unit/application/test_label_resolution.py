"""Unit tests for label resolution in MemoryService._get_cached_labels()."""

from __future__ import annotations

import json

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.domain.entity_extraction import UNIVERSAL_LABELS
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


async def _create_service() -> tuple[MemoryService, SQLiteStore]:
    """Create an in-memory MemoryService for testing."""
    config = NCMSConfig(db_path=":memory:", actr_noise=0.0)
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    return MemoryService(store=store, index=index, graph=graph, config=config), store


class TestGetCachedLabels:
    @pytest.mark.asyncio
    async def test_empty_store_returns_empty(self):
        """No cached labels should return empty dict."""
        svc, store = await _create_service()
        try:
            result = await svc._get_cached_labels(["api"])
            assert result == {}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_cached_labels_loaded(self):
        """Pre-populated labels should be returned."""
        svc, store = await _create_service()
        try:
            labels = ["endpoint", "service", "protocol"]
            await store.set_consolidation_value(
                "entity_labels:api", json.dumps(labels)
            )
            result = await svc._get_cached_labels(["api"])
            assert result == {"api": labels}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_multiple_domains(self):
        """Should load labels for all requested domains."""
        svc, store = await _create_service()
        try:
            await store.set_consolidation_value(
                "entity_labels:api", json.dumps(["endpoint", "service"])
            )
            await store.set_consolidation_value(
                "entity_labels:db", json.dumps(["table", "column"])
            )
            result = await svc._get_cached_labels(["api", "db"])
            assert result == {
                "api": ["endpoint", "service"],
                "db": ["table", "column"],
            }
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_missing_domain_excluded(self):
        """Domains without cached labels should not appear in result."""
        svc, store = await _create_service()
        try:
            await store.set_consolidation_value(
                "entity_labels:api", json.dumps(["endpoint"])
            )
            result = await svc._get_cached_labels(["api", "finance"])
            assert "api" in result
            assert "finance" not in result
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_invalid_json_ignored(self):
        """Corrupted cache entries should be silently ignored."""
        svc, store = await _create_service()
        try:
            await store.set_consolidation_value(
                "entity_labels:api", "not-valid-json"
            )
            result = await svc._get_cached_labels(["api"])
            assert result == {}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_non_list_json_ignored(self):
        """Cache entries that aren't JSON arrays should be ignored."""
        svc, store = await _create_service()
        try:
            await store.set_consolidation_value(
                "entity_labels:api", json.dumps({"not": "a list"})
            )
            result = await svc._get_cached_labels(["api"])
            assert result == {}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_empty_domains_returns_empty(self):
        """Empty domain list should return empty dict."""
        svc, store = await _create_service()
        try:
            result = await svc._get_cached_labels([])
            assert result == {}
        finally:
            await store.close()
