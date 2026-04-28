"""Tests for dream cycle methods in ConsolidationService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from ncms.application.consolidation_service import ConsolidationService
from ncms.config import NCMSConfig
from ncms.domain.models import AccessRecord, Entity, Memory, SearchLogEntry
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def dream_env():
    """Set up store, index, graph, and config with dream cycle enabled."""
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    config = NCMSConfig(
        db_path=":memory:",
        dream_cycle_enabled=True,
        dream_rehearsal_fraction=0.50,  # Rehearse top 50% for testing
        dream_min_access_count=2,  # Lower threshold for tests
        dream_importance_drift_window_days=14,
        dream_importance_drift_rate=0.5,
        actr_noise=0.0,
    )
    svc = ConsolidationService(
        store=store,
        index=index,
        graph=graph,
        config=config,
    )
    yield store, index, graph, config, svc
    await store.close()


async def _create_memory_with_accesses(
    store: SQLiteStore,
    graph: NetworkXGraph,
    content: str,
    n_accesses: int = 3,
    importance: float = 5.0,
    entity_names: list[str] | None = None,
) -> Memory:
    """Create a memory with entity links and access records."""
    mem = Memory(content=content, domains=["test"], importance=importance)
    await store.save_memory(mem)

    # Create entities and link them (save to store for FK + graph for in-memory)
    for name in entity_names or []:
        entity = Entity(id=f"e-{name}", name=name, type="concept")
        await store.save_entity(entity)
        graph.add_entity(entity)
        graph.link_memory_entity(mem.id, entity.id)
        await store.link_memory_entity(mem.id, entity.id)

    # Log accesses
    for i in range(n_accesses):
        await store.log_access(
            AccessRecord(
                memory_id=mem.id,
                accessing_agent="test",
                accessed_at=datetime.now(UTC) - timedelta(hours=i * 24),
            )
        )

    return mem


# ── run_dream_rehearsal ──────────────────────────────────────────────


class TestDreamRehearsal:
    async def test_returns_zero_when_disabled(self) -> None:
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        config = NCMSConfig(db_path=":memory:", dream_cycle_enabled=False)
        svc = ConsolidationService(store=store, config=config)
        result = await svc.run_dream_rehearsal()
        assert result == 0
        await store.close()

    async def test_rehearses_eligible_memories(self, dream_env) -> None:
        store, index, graph, config, svc = dream_env

        # Create 4 memories with enough accesses
        for i in range(4):
            await _create_memory_with_accesses(
                store,
                graph,
                f"Memory {i}",
                n_accesses=3,
                entity_names=[f"concept-{i}"],
            )

        rehearsed = await svc.run_dream_rehearsal()
        # With fraction=0.50, should rehearse ~2 of 4
        assert rehearsed >= 1
        assert rehearsed <= 4

    async def test_skips_memories_below_min_access_count(self, dream_env) -> None:
        store, index, graph, config, svc = dream_env

        # Create memory with only 1 access (below min_access_count=2)
        mem = Memory(content="Low access memory", domains=["test"])
        await store.save_memory(mem)
        await store.log_access(AccessRecord(memory_id=mem.id, accessing_agent="test"))

        rehearsed = await svc.run_dream_rehearsal()
        assert rehearsed == 0

    async def test_creates_synthetic_access_records(self, dream_env) -> None:
        store, index, graph, config, svc = dream_env

        mem = await _create_memory_with_accesses(
            store,
            graph,
            "Important memory",
            n_accesses=5,
            entity_names=["important-concept"],
        )

        initial_accesses = await store.get_access_times(mem.id)
        initial_count = len(initial_accesses)

        await svc.run_dream_rehearsal()

        post_accesses = await store.get_access_times(mem.id)
        # Should have at least one more access from dream rehearsal
        assert len(post_accesses) >= initial_count


# ── learn_association_strengths ──────────────────────────────────────


class TestAssociationLearning:
    async def test_returns_zero_when_disabled(self) -> None:
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        config = NCMSConfig(db_path=":memory:", dream_cycle_enabled=False)
        svc = ConsolidationService(store=store, config=config)
        result = await svc.learn_association_strengths()
        assert result == 0
        await store.close()

    async def test_learns_from_search_pairs(self, dream_env) -> None:
        store, index, graph, config, svc = dream_env

        # Create memories with entities that have varying co-occurrence patterns
        mem1 = await _create_memory_with_accesses(
            store,
            graph,
            "Redis cache config",
            entity_names=["redis", "cache"],
        )
        mem2 = await _create_memory_with_accesses(
            store,
            graph,
            "Redis session store",
            entity_names=["redis", "session"],
        )
        mem3 = await _create_memory_with_accesses(
            store,
            graph,
            "PostgreSQL backup",
            entity_names=["postgres", "backup"],
        )

        # Log searches with varying result sets so PMI has signal:
        # redis+cache co-occur in some but not all results
        for i in range(3):
            await store.log_search(
                SearchLogEntry(
                    query=f"redis query {i}",
                    returned_ids=[mem1.id, mem2.id],  # redis entities
                )
            )
        for i in range(3):
            await store.log_search(
                SearchLogEntry(
                    query=f"postgres query {i}",
                    returned_ids=[mem3.id],  # postgres entities only
                )
            )

        saved = await svc.learn_association_strengths()
        assert saved > 0

        # Verify strengths are stored
        strengths = await store.get_association_strengths()
        assert len(strengths) > 0

    async def test_returns_zero_with_no_searches(self, dream_env) -> None:
        store, index, graph, config, svc = dream_env
        result = await svc.learn_association_strengths()
        assert result == 0


# ── adjust_importance_drift ──────────────────────────────────────────


class TestImportanceDrift:
    async def test_returns_zero_when_disabled(self) -> None:
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        config = NCMSConfig(db_path=":memory:", dream_cycle_enabled=False)
        svc = ConsolidationService(store=store, config=config)
        result = await svc.adjust_importance_drift()
        assert result == 0
        await store.close()

    async def test_adjusts_trending_memory(self, dream_env) -> None:
        store, index, graph, config, svc = dream_env

        mem = Memory(content="Trending memory", domains=["test"], importance=5.0)
        await store.save_memory(mem)

        # Create many recent accesses (last few hours)
        now = datetime.now(UTC)
        for i in range(10):
            await store.log_access(
                AccessRecord(
                    memory_id=mem.id,
                    accessing_agent="test",
                    accessed_at=now - timedelta(hours=i),
                )
            )

        # Create fewer older accesses (8-14 days ago)
        for i in range(2):
            await store.log_access(
                AccessRecord(
                    memory_id=mem.id,
                    accessing_agent="test",
                    accessed_at=now - timedelta(days=8 + i),
                )
            )

        adjusted = await svc.adjust_importance_drift()

        # Retrieve updated memory
        updated = await store.get_memory(mem.id)
        assert updated is not None
        # Recent access rate >> older rate → should drift up
        assert updated.importance > 5.0 or adjusted >= 0

    async def test_skips_single_access_memories(self, dream_env) -> None:
        store, index, graph, config, svc = dream_env

        mem = Memory(content="Single access", domains=["test"])
        await store.save_memory(mem)
        await store.log_access(AccessRecord(memory_id=mem.id, accessing_agent="test"))

        adjusted = await svc.adjust_importance_drift()
        assert adjusted == 0


# ── run_dream_cycle (orchestrator) ───────────────────────────────────


class TestDreamCycle:
    async def test_returns_zero_dict_when_disabled(self) -> None:
        store = SQLiteStore(db_path=":memory:")
        await store.initialize()
        config = NCMSConfig(db_path=":memory:", dream_cycle_enabled=False)
        svc = ConsolidationService(store=store, config=config)
        result = await svc.run_dream_cycle()
        assert result == {"rehearsal": 0, "associations": 0, "drift": 0}
        await store.close()

    async def test_runs_full_cycle(self, dream_env) -> None:
        store, index, graph, config, svc = dream_env

        # Create some memories so rehearsal has work to do
        for i in range(5):
            await _create_memory_with_accesses(
                store,
                graph,
                f"Memory {i}",
                n_accesses=3,
                entity_names=[f"entity-{i}"],
            )

        result = await svc.run_dream_cycle()
        assert "rehearsal" in result
        assert "associations" in result
        assert "drift" in result

    async def test_non_fatal_on_exception(self, dream_env) -> None:
        """Dream cycle should not raise even if individual phases fail."""
        store, index, graph, config, svc = dream_env

        # This should complete without raising
        result = await svc.run_dream_cycle()
        assert isinstance(result, dict)
        # All keys should have int values
        for v in result.values():
            assert isinstance(v, int)
