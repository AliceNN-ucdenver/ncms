"""Integration tests for Phase 4: Knowledge Consolidation.

Tests that the consolidation pipeline discovers cross-memory patterns
via entity co-occurrence clustering and stores them as insight memories.
Insights are indexed in Tantivy and linked to entities in the graph.

All tests mock litellm.acompletion to avoid real LLM calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ncms.application.consolidation_service import ConsolidationService
from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

_PATCH_TARGET = "litellm.acompletion"


def _mock_llm_response(content: str | list | dict) -> MagicMock:
    """Create a mock litellm acompletion response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    if isinstance(content, (list, dict)):
        response.choices[0].message.content = json.dumps(content)
    else:
        response.choices[0].message.content = content
    return response


def _insight_response(
    insight: str = "Emergent pattern discovered",
    pattern_type: str = "dependency",
    confidence: float = 0.8,
    key_entities: list[str] | None = None,
) -> MagicMock:
    """Create a mock response for insight synthesis."""
    return _mock_llm_response({
        "insight": insight,
        "pattern_type": pattern_type,
        "confidence": confidence,
        "key_entities": key_entities or [],
    })


async def _create_services(
    **config_overrides,
) -> tuple[MemoryService, ConsolidationService, SQLiteStore]:
    """Create all services needed for consolidation integration tests.

    Returns (memory_service, consolidation_service, store).
    Caller should ``await store.close()`` when done.
    """
    defaults: dict = dict(
        db_path=":memory:",
        actr_noise=0.0,
        keyword_bridge_enabled=False,  # Disable keyword extraction
        consolidation_knowledge_enabled=True,
        consolidation_knowledge_min_cluster_size=3,
        consolidation_knowledge_model="gpt-4o-mini",
        consolidation_knowledge_max_insights_per_run=5,
    )
    defaults.update(config_overrides)
    config = NCMSConfig(**defaults)

    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()

    memory_svc = MemoryService(store=store, index=index, graph=graph, config=config)
    consolidation_svc = ConsolidationService(
        store=store, index=index, graph=graph, config=config,
    )

    return memory_svc, consolidation_svc, store


async def _store_related_memories(
    svc: MemoryService,
    count: int = 4,
    domain: str = "auth",
) -> list:
    """Store several memories about related auth topics.

    The regex entity extractor will auto-extract technology entities
    (e.g. JWT, PostgreSQL, Redis) creating shared graph links.
    """
    contents = [
        "JWT token validation for API endpoint authentication",
        "JWT refresh tokens stored in PostgreSQL user sessions table",
        "PostgreSQL stores role-based access control permissions",
        "API gateway validates JWT before routing to microservices",
        "Redis caches JWT validation results for performance",
    ]
    memories = []
    for content in contents[:count]:
        mem = await svc.store_memory(content=content, domains=[domain])
        memories.append(mem)
    return memories


class TestConsolidationKnowledge:
    """Integration tests for knowledge consolidation pipeline."""

    @pytest.mark.asyncio
    async def test_consolidate_creates_insight_memories(self):
        """End-to-end: store memories → consolidate → verify insight created."""
        svc, consol, store = await _create_services()
        try:
            # Store memories with shared entities (JWT, PostgreSQL)
            await _store_related_memories(svc, count=4)

            # Run consolidation with mocked LLM
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_insight_response(
                    insight="JWT and PostgreSQL form a tight auth dependency chain.",
                    key_entities=["JWT", "PostgreSQL"],
                ),
            ):
                created = await consol.consolidate_knowledge()

            assert created >= 1, "Should create at least one insight"

            # Verify insight memory exists
            all_memories = await store.list_memories(limit=10000)
            insights = [m for m in all_memories if m.type == "insight"]
            assert len(insights) >= 1

            insight = insights[0]
            assert "JWT" in insight.content or "dependency" in insight.content
            assert insight.structured is not None
            assert "source_memory_ids" in insight.structured
            assert len(insight.structured["source_memory_ids"]) >= 3

        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_insight_indexed_and_searchable(self):
        """Insight memories should appear in BM25 search results."""
        svc, consol, store = await _create_services()
        try:
            await _store_related_memories(svc, count=4)

            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_insight_response(
                    insight="The authentication pipeline depends on JWT tokens "
                    "stored in PostgreSQL for session validation.",
                ),
            ):
                await consol.consolidate_knowledge()

            # Search for the insight via BM25
            results = await svc.search("authentication pipeline JWT session")
            result_types = {r.memory.type for r in results}
            assert "insight" in result_types, (
                "Insight should be discoverable via BM25 search"
            )

        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consolidation_tracks_last_run(self):
        """consolidation_state should be updated after a run."""
        svc, consol, store = await _create_services()
        try:
            await _store_related_memories(svc, count=4)

            # Before first run — no timestamp
            before = await store.get_consolidation_value("last_knowledge_consolidation")
            assert before is None

            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_insight_response(),
            ):
                await consol.consolidate_knowledge()

            # After run — timestamp set
            after = await store.get_consolidation_value("last_knowledge_consolidation")
            assert after is not None

        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_second_run_only_processes_new_memories(self):
        """Incremental: second run should only process memories created after first run."""
        svc, consol, store = await _create_services()
        try:
            # First batch of memories
            await _store_related_memories(svc, count=4)

            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_insight_response(
                    insight="First run insight about auth pipeline.",
                ),
            ):
                first_count = await consol.consolidate_knowledge()

            # Second run with no new memories — should do nothing
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_insight_response(
                    insight="This should not be created.",
                ),
            ) as mock_llm:
                second_count = await consol.consolidate_knowledge()

            # No new memories since last run → 0 insights
            assert second_count == 0

        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consolidation_disabled_no_insights(self):
        """With consolidation_knowledge_enabled=False, no insights should be created."""
        svc, consol, store = await _create_services(
            consolidation_knowledge_enabled=False,
        )
        try:
            await _store_related_memories(svc, count=4)

            result = await consol.consolidate_knowledge()
            assert result == 0

            all_memories = await store.list_memories(limit=10000)
            insights = [m for m in all_memories if m.type == "insight"]
            assert len(insights) == 0

        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consolidation_skips_when_too_few_memories(self):
        """Below min_cluster_size, no work should be done."""
        svc, consol, store = await _create_services(
            consolidation_knowledge_min_cluster_size=10,
        )
        try:
            # Only 4 memories — below min of 10
            await _store_related_memories(svc, count=4)

            result = await consol.consolidate_knowledge()
            assert result == 0

        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_insights_not_re_consolidated(self):
        """Insight memories should be excluded from subsequent consolidation runs."""
        svc, consol, store = await _create_services()
        try:
            await _store_related_memories(svc, count=4)

            # First consolidation — creates insights
            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_insight_response(
                    insight="First insight about auth dependencies.",
                ),
            ):
                first_count = await consol.consolidate_knowledge()

            assert first_count >= 1

            # Reset last_run so second pass sees all memories
            await store.set_consolidation_value(
                "last_knowledge_consolidation", ""
            )

            # Add more memories to meet min_cluster_size again
            await _store_related_memories(svc, count=4, domain="db")

            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                return_value=_insight_response(
                    insight="Second insight should not include previous insights.",
                ),
            ):
                second_count = await consol.consolidate_knowledge()

            # Verify insights from first run are not in clusters of second run
            all_memories = await store.list_memories(limit=10000)
            insights = [m for m in all_memories if m.type == "insight"]
            for insight in insights:
                if insight.structured:
                    source_ids = insight.structured.get("source_memory_ids", [])
                    # No insight should be a source of another insight
                    insight_ids = {i.id for i in insights}
                    overlap = set(source_ids) & insight_ids
                    assert len(overlap) == 0, (
                        "Insights should not be sources for other insights"
                    )

        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_llm_failure_non_fatal(self):
        """LLM error during synthesis should not break consolidation."""
        svc, consol, store = await _create_services()
        try:
            await _store_related_memories(svc, count=4)

            with patch(
                _PATCH_TARGET,
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM API unavailable"),
            ):
                result = await consol.consolidate_knowledge()

            # Should return 0 (no insights created) but not raise
            assert result == 0

            # Original memories should still be intact
            all_memories = await store.list_memories(limit=10000)
            non_insight = [m for m in all_memories if m.type != "insight"]
            assert len(non_insight) == 4

        finally:
            await store.close()
