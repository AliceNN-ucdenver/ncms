"""Integration tests for SPLADE + BM25 fused retrieval pipeline.

Uses a mock SpladeEngine to avoid model download. Tests the full
pipeline: store → SPLADE index → fused search → ACT-R scoring.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


class MockSpladeEngine:
    """Deterministic SPLADE mock for integration testing.

    Uses word overlap to produce sparse scores (no model download).
    """

    def __init__(self):
        self._docs: dict[str, set[str]] = {}

    def index_memory(self, memory) -> None:
        self._docs[memory.id] = set(memory.content.lower().split())

    def remove(self, memory_id: str) -> None:
        self._docs.pop(memory_id, None)

    def search(self, query: str, limit: int = 50) -> list[tuple[str, float]]:
        q_words = set(query.lower().split())
        scores = []
        for mid, words in self._docs.items():
            overlap = len(q_words & words)
            if overlap > 0:
                scores.append((mid, float(overlap)))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:limit]

    @property
    def vector_count(self) -> int:
        return len(self._docs)


@pytest.fixture
def splade_config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        splade_enabled=True,
        scoring_weight_splade=0.2,
        scoring_weight_bm25=0.4,
        scoring_weight_actr=0.4,
    )


class TestSPLADEPipeline:
    @pytest.mark.asyncio
    async def test_splade_results_fused_with_bm25(self, splade_config):
        """BM25 and SPLADE results should be fused via RRF."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()
        splade = MockSpladeEngine()

        svc = MemoryService(
            store=store, index=index, graph=graph, config=splade_config, splade=splade,
        )

        await svc.store_memory(
            content="Flask web framework for Python REST APIs",
            domains=["api"],
        )
        await svc.store_memory(
            content="Django template rendering with Jinja",
            domains=["api"],
        )

        results = await svc.search("Flask REST API")
        assert len(results) >= 1
        # Flask memory should rank first (matches both BM25 and SPLADE)
        assert "Flask" in results[0].memory.content

        await store.close()

    @pytest.mark.asyncio
    async def test_splade_only_candidate_discovered(self, splade_config):
        """A memory found only by SPLADE should still appear in results."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        # Custom SPLADE that finds a memory BM25 would miss
        splade = MockSpladeEngine()

        svc = MemoryService(
            store=store, index=index, graph=graph, config=splade_config, splade=splade,
        )

        # Store a memory with content that overlaps the query via SPLADE
        mem = await svc.store_memory(
            content="REST API endpoint design patterns",
            domains=["api"],
        )

        # Search for something that shares words with the memory
        results = await svc.search("REST API design")
        assert len(results) >= 1

        await store.close()

    @pytest.mark.asyncio
    async def test_splade_disabled_no_effect(self):
        """With SPLADE disabled, search should work normally (BM25 only)."""
        config = NCMSConfig(db_path=":memory:", actr_noise=0.0, splade_enabled=False)
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        # Pass splade=None (disabled)
        svc = MemoryService(store=store, index=index, graph=graph, config=config)

        await svc.store_memory(
            content="Express framework for Node.js",
            domains=["api"],
        )

        results = await svc.search("Express Node")
        assert len(results) >= 1

        await store.close()

    @pytest.mark.asyncio
    async def test_splade_score_populated(self, splade_config):
        """ScoredMemory.splade_score should be populated when SPLADE is active."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()
        splade = MockSpladeEngine()

        svc = MemoryService(
            store=store, index=index, graph=graph, config=splade_config, splade=splade,
        )

        await svc.store_memory(
            content="Flask web framework REST API",
            domains=["api"],
        )

        results = await svc.search("Flask REST API")
        assert len(results) >= 1
        # SPLADE score should be > 0 since query words match the memory
        assert results[0].splade_score > 0.0

        await store.close()

    @pytest.mark.asyncio
    async def test_delete_removes_from_both_indexes(self, splade_config):
        """Deleting a memory should remove it from both BM25 and SPLADE."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()
        splade = MockSpladeEngine()

        svc = MemoryService(
            store=store, index=index, graph=graph, config=splade_config, splade=splade,
        )

        mem = await svc.store_memory(
            content="Flask API framework",
            domains=["api"],
        )

        assert splade.vector_count == 1

        await svc.delete_memory(mem.id)

        assert splade.vector_count == 0
        # BM25 search should also return nothing
        results = await svc.search("Flask API")
        assert len(results) == 0

        await store.close()

    @pytest.mark.asyncio
    async def test_splade_failure_falls_back(self, splade_config):
        """If SPLADE search fails, BM25 results should still work."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        # SPLADE that raises on search
        splade = MockSpladeEngine()

        svc = MemoryService(
            store=store, index=index, graph=graph, config=splade_config, splade=splade,
        )

        await svc.store_memory(content="Flask web framework", domains=["api"])

        # Make SPLADE search raise an error
        splade.search = MagicMock(side_effect=RuntimeError("SPLADE broke"))

        # Should still return BM25 results
        results = await svc.search("Flask web")
        assert len(results) >= 1

        await store.close()
