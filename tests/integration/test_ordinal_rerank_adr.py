"""Integration test for P1b ordinal rerank — ADR-style production workload.

The unit tests in test_ordinal_rerank.py cover the rerank function in
isolation.  This integration test walks through the real
MemoryService.search path end-to-end, reproducing the production
scenario that motivated P1b:

    An agent asks "What was the last decision on the authentication
    flow?" over a small seeded NCMS with several ADRs on overlapping
    topics at different dates.  The correct answer is the
    most-recent ADR on authentication; BM25 by itself may rank an
    older ADR first (because the older one had more keyword matches
    or higher similarity).  P1b reorders the top-K so recency wins.

Single-test fixture; the scenarios inside differ only in query text.
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
async def adr_service() -> MemoryService:
    """NCMS seeded with five ADRs about authentication across dates."""
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()

    # Keep SPLADE and graph out so we isolate the rerank effect.
    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        splade_enabled=False, scoring_weight_splade=0.0,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.0,
        scoring_weight_graph=0.0,
        scoring_weight_recency=0.0,
        scoring_weight_temporal=0.2,
        temporal_enabled=True,
    )
    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
    )
    # Inline indexing path (no background pool) — we want deterministic
    # state at the end of every store_memory call so the fixture is
    # simpler and we don't leak workers on teardown.

    # Five authentication ADRs at different dates, lexically similar
    # so BM25 can't fully disambiguate by keyword match alone.
    adrs = [
        (
            "ADR-001: Initial authentication design uses session cookies "
            "for user authentication.",
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


class TestOrdinalRerankADR:
    """ADR workload: the most-recent / original ADR should rank first."""

    async def test_latest_adr_on_authentication(
        self, adr_service: MemoryService,
    ) -> None:
        """Query with 'latest' must put the most recent ADR at rank 1."""
        results = await adr_service.search(
            query="What is the latest decision on authentication?",
            limit=5,
        )
        assert results, "expected candidates"
        top_content = results[0].memory.content
        assert "ADR-029" in top_content, (
            f"Expected ADR-029 (most recent) at rank 1, got:\n  "
            + "\n  ".join(
                f"{i + 1}. {r.memory.content[:60]}"
                for i, r in enumerate(results)
            )
        )

    async def test_most_recent_change_to_authentication(
        self, adr_service: MemoryService,
    ) -> None:
        """'Most recent' is an ordinal-last synonym."""
        results = await adr_service.search(
            query="What is the most recent change to authentication flow?",
            limit=5,
        )
        assert results
        assert "ADR-029" in results[0].memory.content

    async def test_original_authentication_design(
        self, adr_service: MemoryService,
    ) -> None:
        """Query with 'original' must put the earliest ADR at rank 1."""
        results = await adr_service.search(
            query="What was the original authentication design?",
            limit=5,
        )
        assert results
        top_content = results[0].memory.content
        assert "ADR-001" in top_content, (
            f"Expected ADR-001 (earliest) at rank 1, got:\n  "
            + "\n  ".join(
                f"{i + 1}. {r.memory.content[:60]}"
                for i, r in enumerate(results)
            )
        )

    async def test_first_authentication_change(
        self, adr_service: MemoryService,
    ) -> None:
        """'First' is an ordinal-first synonym."""
        results = await adr_service.search(
            query="What was the first authentication design?",
            limit=5,
        )
        assert results
        assert "ADR-001" in results[0].memory.content

    async def test_non_ordinal_query_is_not_reordered(
        self, adr_service: MemoryService,
    ) -> None:
        """A query without ordinal intent must NOT be reordered by date.

        This is the regression guard: P1b must be a conditional
        operation, not a side effect on every query.
        """
        results = await adr_service.search(
            query="What authentication does the system use?",
            limit=5,
        )
        assert results
        # No strong assertion on exact rank — just that the result
        # is not forced into chronological order.  Extract the order
        # of observed_at timestamps: if P1b fired by accident, they'd
        # be monotonic; they shouldn't be.
        dates = [
            r.memory.observed_at for r in results
            if r.memory.observed_at is not None
        ]
        if len(dates) >= 3:
            monotonic_asc = all(
                dates[i] <= dates[i + 1]
                for i in range(len(dates) - 1)
            )
            monotonic_desc = all(
                dates[i] >= dates[i + 1]
                for i in range(len(dates) - 1)
            )
            assert not (monotonic_asc or monotonic_desc), (
                "non-ordinal query should not be chronologically sorted"
            )
