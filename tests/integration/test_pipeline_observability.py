"""Integration tests for pipeline observability events.

Verifies that MemoryService emits pipeline stage events during
store and search operations when an EventLog is provided.
"""

from __future__ import annotations

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.observability.event_log import EventLog
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
async def pipeline_env():
    """Create a full MemoryService with EventLog for pipeline testing."""
    log = EventLog()
    config = NCMSConfig(db_path=":memory:", actr_noise=0.0)
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        event_log=log,
    )

    yield svc, log, config

    await store.close()


def _pipeline_events(log: EventLog, pipeline_type: str) -> list:
    """Extract pipeline events of a specific type from the event log."""
    return [
        e for e in log.recent(200)
        if e.type.startswith(f"pipeline.{pipeline_type}.")
    ]


def _stages(events: list) -> list[str]:
    """Extract ordered stage names from pipeline events."""
    return [e.data["stage"] for e in reversed(events)]


class TestStorePipelineObservability:
    """Test pipeline events emitted during store_memory()."""

    @pytest.mark.asyncio
    async def test_store_emits_full_pipeline(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory("Flask is a Python web framework", domains=["api"])

        events = _pipeline_events(log, "store")
        stages = _stages(events)

        # Core stages always present
        assert "start" in stages
        assert "persist" in stages
        assert "bm25_index" in stages
        assert "entity_extraction" in stages
        assert "graph_linking" in stages
        assert "complete" in stages

        # All events share the same pipeline_id
        pipeline_ids = {e.data["pipeline_id"] for e in events}
        assert len(pipeline_ids) == 1

    @pytest.mark.asyncio
    async def test_store_pipeline_has_timing(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory("SQLAlchemy ORM patterns", domains=["db"])

        events = _pipeline_events(log, "store")
        for event in events:
            assert "duration_ms" in event.data
            assert event.data["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_store_complete_has_total_duration(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory("React component lifecycle", domains=["frontend"])

        events = _pipeline_events(log, "store")
        complete = [e for e in events if e.data["stage"] == "complete"]
        assert len(complete) == 1
        assert complete[0].data["total_duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_store_entity_extraction_has_counts(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory(
            "FastAPI uses Pydantic for data validation",
            domains=["api"],
        )

        events = _pipeline_events(log, "store")
        extraction = [e for e in events if e.data["stage"] == "entity_extraction"]
        assert len(extraction) == 1
        data = extraction[0].data
        assert "auto_count" in data
        assert "total_count" in data
        assert data["total_count"] >= 1  # At least "FastAPI" or "Pydantic" extracted

    @pytest.mark.asyncio
    async def test_store_backward_compat(self, pipeline_env):
        """Both pipeline events AND memory.stored should be emitted."""
        svc, log, _ = pipeline_env
        await svc.store_memory("Test backward compat", domains=["test"])

        pipeline_events = _pipeline_events(log, "store")
        memory_stored = [e for e in log.recent(200) if e.type == "memory.stored"]

        assert len(pipeline_events) >= 5  # start + persist + bm25 + entity + graph + complete
        assert len(memory_stored) == 1

    @pytest.mark.asyncio
    async def test_splade_stage_skipped_when_disabled(self, pipeline_env):
        """When SPLADE is not enabled, splade_index stage should not appear."""
        svc, log, _ = pipeline_env
        await svc.store_memory("No SPLADE here", domains=["test"])

        stages = _stages(_pipeline_events(log, "store"))
        assert "splade_index" not in stages


class TestSearchPipelineObservability:
    """Test pipeline events emitted during search()."""

    @pytest.mark.asyncio
    async def test_search_emits_full_pipeline(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory("Flask web framework for Python REST APIs")

        # Clear store events so we only see search events
        log.count()

        await svc.search("Flask API")

        all_events = log.recent(200)
        search_events = [
            e for e in all_events
            if e.type.startswith("pipeline.search.")
        ]
        stages = _stages(search_events)

        assert "start" in stages
        assert "bm25" in stages
        assert "entity_extraction" in stages
        assert "actr_scoring" in stages
        assert "complete" in stages

        # All search events share the same pipeline_id
        pipeline_ids = {e.data["pipeline_id"] for e in search_events}
        assert len(pipeline_ids) == 1

    @pytest.mark.asyncio
    async def test_search_bm25_has_candidate_count(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory("Flask web framework")
        await svc.store_memory("Django web framework")

        await svc.search("web framework")

        search_events = [
            e for e in log.recent(200)
            if e.type.startswith("pipeline.search.")
        ]
        bm25 = [e for e in search_events if e.data["stage"] == "bm25"]
        assert len(bm25) == 1
        assert bm25[0].data["candidate_count"] >= 1

    @pytest.mark.asyncio
    async def test_search_actr_has_aggregate_stats(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory("JWT authentication tokens for APIs")

        await svc.search("JWT auth")

        search_events = [
            e for e in log.recent(200)
            if e.type.startswith("pipeline.search.")
        ]
        actr = [e for e in search_events if e.data["stage"] == "actr_scoring"]
        assert len(actr) == 1
        data = actr[0].data
        assert "candidates_scored" in data
        assert "passed_threshold" in data
        assert "filtered_below_threshold" in data
        assert data["candidates_scored"] >= 1

    @pytest.mark.asyncio
    async def test_search_backward_compat(self, pipeline_env):
        """Both pipeline events AND memory.searched should be emitted."""
        svc, log, _ = pipeline_env
        await svc.store_memory("PostgreSQL database queries")

        await svc.search("PostgreSQL")

        search_pipeline = [
            e for e in log.recent(200)
            if e.type.startswith("pipeline.search.")
        ]
        memory_searched = [e for e in log.recent(200) if e.type == "memory.searched"]

        assert len(search_pipeline) >= 4  # start + bm25 + entity + actr + complete
        assert len(memory_searched) == 1


class TestPipelineCorrelation:
    """Test pipeline ID correlation across operations."""

    @pytest.mark.asyncio
    async def test_separate_stores_have_different_ids(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory("First memory about Flask")
        await svc.store_memory("Second memory about Django")

        store_events = _pipeline_events(log, "store")
        pipeline_ids = {e.data["pipeline_id"] for e in store_events}

        # Two store operations should produce two distinct pipeline_ids
        assert len(pipeline_ids) == 2

    @pytest.mark.asyncio
    async def test_store_and_search_have_different_ids(self, pipeline_env):
        svc, log, _ = pipeline_env
        await svc.store_memory("Redis caching patterns")
        await svc.search("Redis")

        store_ids = {
            e.data["pipeline_id"]
            for e in log.recent(200)
            if e.type.startswith("pipeline.store.")
        }
        search_ids = {
            e.data["pipeline_id"]
            for e in log.recent(200)
            if e.type.startswith("pipeline.search.")
        }

        # Store and search should use completely different pipeline_ids
        assert store_ids.isdisjoint(search_ids)

    @pytest.mark.asyncio
    async def test_no_event_log_still_works(self):
        """MemoryService without event_log doesn't crash."""
        config = NCMSConfig(db_path=":memory:", actr_noise=0.0)
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()

        svc = MemoryService(store=store, index=index, graph=graph, config=config)
        await svc.store_memory("No event log, still works")
        results = await svc.search("event log")
        assert len(results) >= 1

        await store.close()
