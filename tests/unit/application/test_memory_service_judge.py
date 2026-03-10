"""Tests for LLM-as-judge integration in MemoryService search pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
def judge_config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        llm_judge_enabled=True,
        llm_model="test-model",
        tier3_judge_top_k=5,
    )


class TestLLMJudgeIntegration:
    @pytest.mark.asyncio
    async def test_judge_reranks_results(self, judge_config):
        """When LLM judge is enabled, it should rerank candidates."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()
        svc = MemoryService(store=store, index=index, graph=graph, config=judge_config)

        # Store two memories
        mem_a = await svc.store_memory(
            content="Flask web framework for Python REST APIs",
            domains=["api"],
        )
        mem_b = await svc.store_memory(
            content="Flask template rendering and Jinja support",
            domains=["api"],
        )

        # Mock judge_relevance to prefer mem_b over mem_a
        async def mock_judge(query, candidates, model=""):
            return [
                (mem_b.id, 0.95),
                (mem_a.id, 0.30),
            ]

        with patch(
            "ncms.infrastructure.llm.judge.judge_relevance",
            side_effect=mock_judge,
        ):
            results = await svc.search("Flask API")

        assert len(results) >= 2
        # Judge should have reranked: mem_b should now be first
        assert results[0].memory.id == mem_b.id

        await store.close()

    @pytest.mark.asyncio
    async def test_judge_fallback_on_error(self, judge_config):
        """When LLM judge fails, search should still return results."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()
        svc = MemoryService(store=store, index=index, graph=graph, config=judge_config)

        await svc.store_memory(
            content="Express framework for Node.js applications",
            domains=["api"],
        )

        # Mock judge to return fallback (simulating internal error handling)
        async def mock_judge_fallback(query, candidates, model=""):
            return [(c.memory.id, c.total_activation) for c in candidates]

        with patch(
            "ncms.infrastructure.llm.judge.judge_relevance",
            side_effect=mock_judge_fallback,
        ):
            results = await svc.search("Express Node")

        # Should still return results
        assert len(results) >= 1

        await store.close()

    @pytest.mark.asyncio
    async def test_judge_not_called_when_disabled(self):
        """When LLM judge is disabled, judge_relevance should not be invoked."""
        config = NCMSConfig(
            db_path=":memory:",
            actr_noise=0.0,
            llm_judge_enabled=False,
        )
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()
        svc = MemoryService(store=store, index=index, graph=graph, config=config)

        await svc.store_memory(
            content="Django framework for web applications",
            domains=["api"],
        )

        mock_judge = AsyncMock()
        with patch(
            "ncms.infrastructure.llm.judge.judge_relevance",
            mock_judge,
        ):
            results = await svc.search("Django web")

        # Judge should NOT have been called since it's disabled
        mock_judge.assert_not_called()
        assert len(results) >= 1

        await store.close()
