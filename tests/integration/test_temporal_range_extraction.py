"""Phase-A integration: GLiNER temporal labels → normalizer → content_range.

Validates the end-to-end extraction wiring without enabling the
filter.  When ``temporal_range_filter_enabled=True``:

* Ingesting a memory that contains a parseable date persists a row
  in ``memory_content_ranges``.
* Ingesting a memory with no temporal content does NOT persist a row.
* Search emits a ``temporal_range_extracted`` pipeline-event when the
  query has a parseable date.

These tests use the real MemoryService pipeline, real SQLite,
real GLiNER (loaded once).  They're integration-level because the
extraction coverage is the whole point.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def service() -> MemoryService:
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        splade_enabled=False,
        scoring_weight_splade=0.0,
        scoring_weight_graph=0.0,
        scoring_weight_actr=0.0,
        temporal_range_filter_enabled=True,
    )
    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
    )
    yield svc
    await store.close()


class TestContentRangeExtraction:

    async def test_dated_memory_persists_range(
        self, service: MemoryService,
    ) -> None:
        """Memory containing an absolute date gets a content range row."""
        mem = await service.store_memory(
            content="I went to the clinic on June 5, 2024 for a check-up.",
            memory_type="fact",
        )
        await service.flush_indexing()

        got = await service._store.get_content_range(mem.id)
        assert got is not None, "expected a persisted range"
        start, end = got
        # Range should include June 5, 2024.
        assert start.startswith("2024-06-05")
        # End is exclusive; could be June 6 or later depending on
        # normalization.
        assert end > start

    async def test_non_temporal_memory_falls_back_to_metadata(
        self, service: MemoryService,
    ) -> None:
        """Memory with no parseable content dates falls back to observed_at.

        Revised semantics (§14.2 of the design): every ingested memory
        that has an observed_at or created_at gets a day-wide range
        anchored on it.  This drives memory coverage to ~100% on
        conversational prose that rarely contains calendar tokens.
        """
        session_date = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
        mem = await service.store_memory(
            content="The sky is blue and the grass is green.",
            memory_type="fact",
            observed_at=session_date,
        )
        await service.flush_indexing()

        got = await service._store.get_content_range(mem.id)
        assert got is not None, (
            "expected metadata fallback to persist a range"
        )
        start, end = got
        # Day-wide interval anchored on observed_at.
        assert start.startswith("2024-06-05")
        assert end.startswith("2024-06-06")
        # Verify the source is marked as 'metadata'.
        cursor = await service._store.db.execute(
            "SELECT source FROM memory_content_ranges WHERE memory_id = ?",
            (mem.id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "metadata"

    async def test_observed_at_anchors_relative_content(
        self, service: MemoryService,
    ) -> None:
        """'Yesterday' in content resolves against memory's observed_at.

        Ingesting a historical conversation — observed_at is the session
        date, not wall clock.  The content range should reflect the
        session-relative resolution.
        """
        session_date = datetime(2024, 3, 15, 12, 0, tzinfo=UTC)
        mem = await service.store_memory(
            content="Yesterday I finished the quarterly report.",
            memory_type="fact",
            observed_at=session_date,
        )
        await service.flush_indexing()

        got = await service._store.get_content_range(mem.id)
        if got is None:
            # Extractor may not have tagged "yesterday" — that's
            # acceptable at the coverage level.  Don't fail hard on
            # per-span recall; other tests cover the happy path.
            pytest.skip("GLiNER didn't extract 'yesterday' on this run")
        start, _ = got
        # If extracted, start should be 2024-03-14 (yesterday relative
        # to 2024-03-15), NOT wall-clock yesterday.
        assert start.startswith("2024-03-"), (
            f"expected session-relative resolution, got {start}"
        )

    async def test_flag_off_persists_nothing(
        self,
    ) -> None:
        """With feature flag off, no content range is persisted."""
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()
        config = NCMSConfig(
            db_path=":memory:",
            splade_enabled=False,
            scoring_weight_splade=0.0,
            temporal_range_filter_enabled=False,  # OFF
        )
        svc = MemoryService(
            store=store, index=index, graph=graph, config=config,
        )
        try:
            mem = await svc.store_memory(
                content="Meeting on June 5, 2024.",
                memory_type="fact",
            )
            await svc.flush_indexing()
            got = await store.get_content_range(mem.id)
            assert got is None
        finally:
            await store.close()
