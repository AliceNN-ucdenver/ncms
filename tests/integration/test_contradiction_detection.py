"""Integration tests for contradiction detection during store_memory.

Mocks the LLM call but uses real services (SQLite, Tantivy, NetworkX).

Contradiction detection runs as a deferred (fire-and-forget) async task
after store_memory returns.  Tests yield control to the event loop so
the deferred task can complete before asserting.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
def contradiction_config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        contradiction_detection_enabled=True,
        contradiction_candidate_limit=5,
        llm_model="test-model",
    )


class TestContradictionDetection:
    @pytest.mark.asyncio
    async def test_contradiction_detected_and_stored(self, contradiction_config):
        """When a contradiction is detected, it should be stored in structured."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(
            store=store,
            index=index,
            graph=graph,
            config=contradiction_config,
        )

        # Store the first memory
        old_mem = await svc.store_memory(
            content="The API uses session cookies for authentication",
            domains=["api"],
        )

        # Mock contradiction detector to find a contradiction
        async def mock_detect(new_memory, existing_memories, model="", **kwargs):
            for em in existing_memories:
                if em.id == old_mem.id:
                    return [
                        {
                            "existing_memory_id": old_mem.id,
                            "contradiction_type": "configuration",
                            "explanation": "Auth method differs",
                            "severity": "high",
                        }
                    ]
            return []

        with patch(
            "ncms.infrastructure.llm.contradiction_detector.detect_contradictions",
            side_effect=mock_detect,
        ):
            new_mem = await svc.store_memory(
                content="The API uses JWT tokens for authentication",
                domains=["api"],
            )
            # Wait for deferred contradiction task to complete
            await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        # New memory should have contradiction annotation
        updated_new = await store.get_memory(new_mem.id)
        assert updated_new is not None
        assert updated_new.structured is not None
        assert "contradictions" in updated_new.structured
        assert len(updated_new.structured["contradictions"]) == 1
        assert updated_new.structured["contradictions"][0]["existing_memory_id"] == old_mem.id

        await store.close()

    @pytest.mark.asyncio
    async def test_existing_memory_annotated_contradicted_by(self, contradiction_config):
        """The contradicted existing memory should be annotated with contradicted_by."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(
            store=store,
            index=index,
            graph=graph,
            config=contradiction_config,
        )

        old_mem = await svc.store_memory(
            content="Database uses PostgreSQL",
            domains=["database"],
        )

        async def mock_detect(new_memory, existing_memories, model="", **kwargs):
            for em in existing_memories:
                if em.id == old_mem.id:
                    return [
                        {
                            "existing_memory_id": old_mem.id,
                            "contradiction_type": "factual",
                            "explanation": "Database technology differs",
                            "severity": "high",
                        }
                    ]
            return []

        with patch(
            "ncms.infrastructure.llm.contradiction_detector.detect_contradictions",
            side_effect=mock_detect,
        ):
            new_mem = await svc.store_memory(
                content="Database uses MySQL",
                domains=["database"],
            )
            # Wait for deferred contradiction task to complete
            await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        # Old memory should have contradicted_by annotation
        updated_old = await store.get_memory(old_mem.id)
        assert updated_old is not None
        assert updated_old.structured is not None
        assert "contradicted_by" in updated_old.structured
        assert len(updated_old.structured["contradicted_by"]) == 1
        assert updated_old.structured["contradicted_by"][0]["newer_memory_id"] == new_mem.id

        await store.close()

    @pytest.mark.asyncio
    async def test_no_contradiction_no_annotation(self, contradiction_config):
        """When no contradiction is detected, structured should not be modified."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(
            store=store,
            index=index,
            graph=graph,
            config=contradiction_config,
        )

        await svc.store_memory(
            content="Flask uses Jinja2 templates",
            domains=["api"],
        )

        async def mock_detect(new_memory, existing_memories, model="", **kwargs):
            return []

        with patch(
            "ncms.infrastructure.llm.contradiction_detector.detect_contradictions",
            side_effect=mock_detect,
        ):
            new_mem = await svc.store_memory(
                content="Flask supports Werkzeug routing",
                domains=["api"],
            )
            # Wait for deferred contradiction task to complete
            await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        updated = await store.get_memory(new_mem.id)
        assert updated is not None
        # No contradictions found — structured should be unchanged
        if updated.structured:
            assert "contradictions" not in updated.structured

        await store.close()

    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        """With contradiction_detection_enabled=False, no LLM calls should happen."""
        config = NCMSConfig(
            db_path=":memory:",
            actr_noise=0.0,
            contradiction_detection_enabled=False,
        )
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(store=store, index=index, graph=graph, config=config)

        await svc.store_memory(content="Python 3.12", domains=["dev"])

        mock_detect = AsyncMock()
        with patch(
            "ncms.infrastructure.llm.contradiction_detector.detect_contradictions",
            mock_detect,
        ):
            await svc.store_memory(content="Python 3.11", domains=["dev"])

        # Should NOT have been called since detection is disabled
        mock_detect.assert_not_called()

        await store.close()

    @pytest.mark.asyncio
    async def test_llm_failure_does_not_block_store(self, contradiction_config):
        """LLM failure should not prevent the memory from being stored."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(
            store=store,
            index=index,
            graph=graph,
            config=contradiction_config,
        )

        await svc.store_memory(content="first memory", domains=["test"])

        async def mock_detect_error(new_memory, existing_memories, model="", **kwargs):
            raise RuntimeError("LLM unavailable")

        with patch(
            "ncms.infrastructure.llm.contradiction_detector.detect_contradictions",
            side_effect=mock_detect_error,
        ):
            new_mem = await svc.store_memory(
                content="second memory",
                domains=["test"],
            )
            # Wait for deferred contradiction task to complete
            await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        # Memory should still be stored despite LLM failure
        stored = await store.get_memory(new_mem.id)
        assert stored is not None
        assert stored.content == "second memory"

        await store.close()

    @pytest.mark.asyncio
    async def test_domain_scoping(self, contradiction_config):
        """Contradiction check should only consider memories in overlapping domains."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(
            store=store,
            index=index,
            graph=graph,
            config=contradiction_config,
        )

        # Memory in a different domain
        await svc.store_memory(
            content="Server runs on port 3000",
            domains=["frontend"],
        )

        async def mock_detect(new_memory, existing_memories, model="", **kwargs):
            # Should not receive the frontend memory when storing to "backend"
            for em in existing_memories:
                if "frontend" in (em.domains or []):
                    return [
                        {
                            "existing_memory_id": em.id,
                            "contradiction_type": "factual",
                            "explanation": "should not happen",
                            "severity": "high",
                        }
                    ]
            return []

        with patch(
            "ncms.infrastructure.llm.contradiction_detector.detect_contradictions",
            side_effect=mock_detect,
        ):
            new_mem = await svc.store_memory(
                content="Server runs on port 8080",
                domains=["backend"],
            )
            # Wait for deferred contradiction task to complete
            await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        # No contradiction should be found due to domain scoping
        updated = await store.get_memory(new_mem.id)
        if updated and updated.structured:
            assert "contradictions" not in updated.structured

        await store.close()
