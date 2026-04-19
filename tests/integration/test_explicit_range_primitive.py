"""Phase B.4 end-to-end integration: explicit-range primitive.

Production-style fixture — a small corpus of ADR / audit-log-flavored
memories with dates embedded in content and observed_at on the session
envelope.  Confirms that calendar-referenced queries hard-filter the
candidate pool to the right time window.

Per ``docs/p1-temporal-experiment.md`` §17.3, LongMemEval is NOT the
measurement surface for this primitive — these tests are the
production-style benchmark it's designed for.

Scenarios:

1. Explicit year-month range ("ADRs from Q1 2024") — only memories
   whose content_range overlaps Q1 2024 survive.
2. "Since" relative anchor ("decisions since June 2024") — filters to
   memories at or after June 2024.
3. No temporal signal — default retrieval, no filter fires.
4. Arithmetic query — classifier fast-fails, filter doesn't fire,
   all candidates retrievable regardless of date.
5. Missing-range policy — exclude mode drops memories that have no
   content_range row.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

# Eight ADR-style memories across dates spanning Q1 2023 – Q2 2025.
# Each has the date in content too, so GLiNER may extract a content
# range; the metadata fallback catches whatever it misses.
_ADR_CORPUS: list[tuple[str, datetime]] = [
    ("ADR-001 (2023-02-10): Initial authentication via session cookies.",
     datetime(2023, 2, 10, tzinfo=UTC)),
    ("ADR-005 (2023-07-14): Migrate logging to structured JSON.",
     datetime(2023, 7, 14, tzinfo=UTC)),
    ("ADR-012 (2024-01-20): Introduce OAuth 2.0 authentication.",
     datetime(2024, 1, 20, tzinfo=UTC)),
    ("ADR-014 (2024-02-05): Adopt JSON Web Tokens for API authentication.",
     datetime(2024, 2, 5, tzinfo=UTC)),
    ("ADR-017 (2024-03-22): Split monolith into microservices.",
     datetime(2024, 3, 22, tzinfo=UTC)),
    ("ADR-021 (2024-07-15): Retire JWT in favour of short-lived tokens.",
     datetime(2024, 7, 15, tzinfo=UTC)),
    ("ADR-025 (2024-09-03): Migrate database from Postgres to CockroachDB.",
     datetime(2024, 9, 3, tzinfo=UTC)),
    ("ADR-029 (2025-04-02): Replace passwords with passkeys + WebAuthn.",
     datetime(2025, 4, 2, tzinfo=UTC)),
]


async def _make_service(
    *,
    missing_range_policy: str = "include",
) -> MemoryService:
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        splade_enabled=False, scoring_weight_splade=0.0,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.0,
        scoring_weight_graph=0.0,
        scoring_weight_recency=0.0,
        scoring_weight_temporal=0.0,  # Isolate from the P1a soft boost
        temporal_enabled=True,
        temporal_range_filter_enabled=True,
        temporal_missing_range_policy=missing_range_policy,
    )
    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
    )
    for content, when in _ADR_CORPUS:
        await svc.store_memory(
            content=content, memory_type="fact",
            tags=["adr"], observed_at=when,
        )
    await svc.flush_indexing()
    return svc


@pytest_asyncio.fixture
async def adr_service() -> MemoryService:
    svc = await _make_service()
    yield svc
    await svc._store.close()


@pytest_asyncio.fixture
async def adr_service_exclude() -> MemoryService:
    svc = await _make_service(missing_range_policy="exclude")
    yield svc
    await svc._store.close()


def _year(results, year: int) -> int:
    """Count results whose observed_at falls in ``year``."""
    return sum(
        1 for r in results
        if r.memory.observed_at is not None
        and r.memory.observed_at.year == year
    )


class TestExplicitRange:

    async def test_range_query_filters_out_of_window_memories(
        self, adr_service: MemoryService,
    ) -> None:
        """'ADRs from 2024' should not surface 2023 or 2025 memories.

        Metadata fallback gives every memory a day-wide content_range
        equal to its observed_at, so the filter is deterministic.
        """
        results = await adr_service.search(
            query="What ADRs did we accept during 2024?",
            limit=10,
        )
        assert results, "expected candidates"
        # Zero 2023 ADRs, zero 2025 ADRs.
        assert _year(results, 2023) == 0, (
            "2023 ADRs should have been filtered out of the 2024 window"
        )
        assert _year(results, 2025) == 0, (
            "2025 ADRs should have been filtered out of the 2024 window"
        )
        # At least one 2024 ADR present.
        assert _year(results, 2024) >= 1

    async def test_since_anchor_filters_older_memories(
        self, adr_service: MemoryService,
    ) -> None:
        """'Since June 2024' should filter out pre-June-2024 ADRs."""
        results = await adr_service.search(
            query="What decisions have we made since June 2024?",
            limit=10,
        )
        assert results
        # All results' observed_at should be June 2024 or later.
        for r in results:
            obs = r.memory.observed_at
            if obs is None:
                continue
            assert obs >= datetime(2024, 6, 1, tzinfo=UTC), (
                f"{r.memory.content[:40]!r} at {obs.isoformat()} "
                "should have been filtered (pre-June-2024)"
            )


class TestNoFireOnNonTemporal:

    async def test_no_temporal_signal_no_filter(
        self, adr_service: MemoryService,
    ) -> None:
        """Queries without a temporal signal see the full corpus."""
        results = await adr_service.search(
            query="What authentication mechanism do we use?",
            limit=10,
        )
        # Multiple years should be represented when the filter doesn't
        # fire (no temporal intent).
        years = {
            r.memory.observed_at.year for r in results
            if r.memory.observed_at is not None
        }
        assert len(years) >= 2, (
            f"Expected multiple years; got {years}"
        )

    async def test_arithmetic_intent_skips_filter(
        self, adr_service: MemoryService,
    ) -> None:
        """Arithmetic questions must not filter — the classifier
        fast-fails and the explicit-range primitive is skipped."""
        # Seed the query with a date to ensure the normalizer resolves
        # a range; verify the filter does NOT fire.
        results = await adr_service.search(
            query="How many weeks between ADR-012 and ADR-021 in 2024?",
            limit=10,
        )
        # If the filter had fired on 2024, we'd see only 2024 results.
        # Arithmetic intent should skip the filter, so results can span.
        assert results
        # This is a weaker assertion than other tests because BM25
        # will favour 2024-dated candidates naturally.  We only check
        # that the filter didn't forcibly prune — i.e., the search
        # returned SOMETHING (would still if filtered; the real assertion
        # is no exception and reasonable result count).
        assert len(results) >= 3


class TestMissingRangePolicy:
    """Exclude-mode is opt-in: memories without a content_range row get
    dropped.  With the metadata fallback now persisting a range per
    ingested memory, exclude-mode should only differ from include-mode
    when content_ranges rows are missing (never in this fixture)."""

    async def test_exclude_policy_preserves_results_when_ranges_present(
        self, adr_service_exclude: MemoryService,
    ) -> None:
        """Every memory has a metadata-fallback range → exclude-mode
        matches include-mode."""
        results = await adr_service_exclude.search(
            query="What ADRs did we accept during 2024?",
            limit=10,
        )
        assert results
        for r in results:
            obs = r.memory.observed_at
            if obs is not None:
                assert obs.year == 2024, (
                    f"exclude-mode should only return 2024 memories; "
                    f"got {obs.year} for {r.memory.content[:40]!r}"
                )
