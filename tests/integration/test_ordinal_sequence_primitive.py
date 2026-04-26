"""Phase B.2 end-to-end integration: ordinal-sequence primitive.

Walks the real ``MemoryService.search`` pipeline with five seeded
ADR memories at different dates.  With ``temporal_range_filter_enabled``
on, the temporal-intent classifier should detect ORDINAL_SINGLE on
queries like "What is the latest decision on authentication?" and the
retrieval pipeline should reorder the top-K by ``observed_at``.

Differs from the retired ADR regression test: the rerank now fires
only when the classifier confirms ordinal intent, not on every
"first" / "last" keyword, and uses classified graph entity linkage
plus the metadata fallback range so memory-side coverage is 100%.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def adr_service() -> MemoryService:
    """NCMS seeded with five authentication ADRs across dates."""
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
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.0,
        scoring_weight_graph=0.0,
        scoring_weight_recency=0.0,
        scoring_weight_temporal=0.2,
        temporal_enabled=True,
        temporal_range_filter_enabled=True,
    )
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=config,
    )

    adrs = [
        (
            "ADR-001: Initial authentication design uses session cookies for user authentication.",
            datetime(2023, 1, 15, tzinfo=UTC),
        ),
        (
            "ADR-007: Authentication refactored to use OAuth 2.0 flow "
            "with third-party identity providers.",
            datetime(2023, 6, 3, tzinfo=UTC),
        ),
        (
            "ADR-014: Authentication adds JWT bearer tokens alongside "
            "OAuth for API authentication.",
            datetime(2024, 2, 20, tzinfo=UTC),
        ),
        (
            "ADR-021: Authentication supersedes JWT with short-lived "
            "access tokens and refresh tokens.",
            datetime(2025, 3, 10, tzinfo=UTC),
        ),
        (
            "ADR-029: Authentication flow latest: passkeys + WebAuthn "
            "replacing password authentication entirely.",
            datetime(2026, 1, 8, tzinfo=UTC),
        ),
    ]
    for content, when in adrs:
        await svc.store_memory(
            content=content,
            memory_type="fact",
            tags=["adr", "auth"],
            observed_at=when,
        )
    await svc.flush_indexing()
    yield svc
    await store.close()


class TestOrdinalSingle:
    """Single-subject ordinal: 'latest X' / 'first X' with one subject."""

    async def test_latest_on_authentication_surfaces_newest(
        self,
        adr_service: MemoryService,
    ) -> None:
        results = await adr_service.search(
            query="What is the latest decision on authentication?",
            limit=5,
        )
        assert results, "expected candidates"
        # ADR-029 is newest (2026-01-08); the ordinal-single primitive
        # should sort subject-linked memories by observed_at desc.
        top_content = results[0].memory.content
        assert "ADR-029" in top_content, "Expected ADR-029 at rank 1; got:\n  " + "\n  ".join(
            f"{i + 1}. {r.memory.content[:60]}" for i, r in enumerate(results)
        )

    async def test_original_authentication_surfaces_oldest(
        self,
        adr_service: MemoryService,
    ) -> None:
        results = await adr_service.search(
            query="What was the original authentication design?",
            limit=5,
        )
        assert results
        # ADR-001 is oldest (2023-01-15); ordinal=first sort ascending.
        assert "ADR-001" in results[0].memory.content


class TestNoOrdinalNoOp:
    """Queries without ordinal intent don't get reordered by date."""

    async def test_plain_authentication_query_not_chronological(
        self,
        adr_service: MemoryService,
    ) -> None:
        """Plain 'what authentication does the system use' should not
        force a date ordering — the temporal-intent classifier returns
        NONE, so the ordinal primitive is a no-op."""
        results = await adr_service.search(
            query="What authentication does the system use?",
            limit=5,
        )
        assert results
        dates = [r.memory.observed_at for r in results if r.memory.observed_at is not None]
        if len(dates) >= 3:
            monotonic_asc = all(dates[i] <= dates[i + 1] for i in range(len(dates) - 1))
            monotonic_desc = all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1))
            assert not (monotonic_asc or monotonic_desc), (
                "non-ordinal query should not be chronologically sorted"
            )


class TestFlagOff:
    """With the flag off, the ordinal primitive never fires."""

    async def test_ordinal_primitive_gated_off(self) -> None:
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        index = TantivyEngine()
        index.initialize()
        graph = NetworkXGraph()
        config = NCMSConfig(
            db_path=":memory:",
            splade_enabled=False,
            scoring_weight_splade=0.0,
            temporal_enabled=True,
            temporal_range_filter_enabled=False,  # OFF
        )
        svc = MemoryService(
            store=store,
            index=index,
            graph=graph,
            config=config,
        )
        try:
            # Seed two ADRs; query with ordinal intent.
            await svc.store_memory(
                content="ADR-001 authentication uses cookies",
                memory_type="fact",
                observed_at=datetime(2023, 1, 1, tzinfo=UTC),
            )
            await svc.store_memory(
                content="ADR-029 authentication uses passkeys",
                memory_type="fact",
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            await svc.flush_indexing()

            results = await svc.search(
                query="What is the latest authentication?",
                limit=5,
            )
            # With primitive off, top result is whatever BM25 picks —
            # we don't assert chronological order.  Just assert no
            # crash, and that we got results.
            assert results
        finally:
            await store.close()
