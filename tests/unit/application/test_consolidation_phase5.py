"""Tests for Phase 5 hierarchical consolidation in ConsolidationService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from ncms.application.consolidation_service import ConsolidationService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    EdgeType,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest_asyncio.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def index():
    engine = TantivyEngine()
    engine.initialize()
    return engine


def _episode_config(**overrides) -> NCMSConfig:
    defaults = {
        "db_path": ":memory:",
        "episode_consolidation_enabled": True,
        "consolidation_knowledge_model": "test-model",
        "consolidation_knowledge_api_base": None,
        "consolidation_max_abstracts_per_run": 10,
        "abstract_refresh_days": 7,
    }
    defaults.update(overrides)
    return NCMSConfig(**defaults)


def _trajectory_config(**overrides) -> NCMSConfig:
    defaults = {
        "db_path": ":memory:",
        "trajectory_consolidation_enabled": True,
        "trajectory_min_transitions": 3,
        "consolidation_knowledge_model": "test-model",
        "consolidation_knowledge_api_base": None,
        "consolidation_max_abstracts_per_run": 10,
        "abstract_refresh_days": 7,
    }
    defaults.update(overrides)
    return NCMSConfig(**defaults)


def _pattern_config(**overrides) -> NCMSConfig:
    defaults = {
        "db_path": ":memory:",
        "pattern_consolidation_enabled": True,
        "pattern_min_episodes": 3,
        "pattern_entity_overlap_threshold": 0.3,
        "pattern_stability_threshold": 0.7,
        "consolidation_knowledge_model": "test-model",
        "consolidation_knowledge_api_base": None,
        "consolidation_max_abstracts_per_run": 10,
        "abstract_refresh_days": 7,
    }
    defaults.update(overrides)
    return NCMSConfig(**defaults)


# ── Phase 5A: Episode Consolidation ─────────────────────────────────────


class TestConsolidateEpisodes:
    """Tests for consolidate_episodes (Phase 5A)."""

    async def test_disabled_returns_zero(self, store, index) -> None:
        config = NCMSConfig(db_path=":memory:", episode_consolidation_enabled=False)
        svc = ConsolidationService(store=store, index=index, config=config)
        assert await svc.consolidate_episodes() == 0

    async def test_no_closed_episodes_returns_zero(self, store, index) -> None:
        config = _episode_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        assert await svc.consolidate_episodes() == 0

    @patch(
        "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
        new_callable=AsyncMock,
    )
    async def test_creates_summary_from_closed_episode(
        self, mock_llm, store, index,
    ) -> None:
        mock_llm.return_value = {
            "summary": "API migration completed successfully.",
            "actors": ["api-team"],
            "artifacts": ["api-v2"],
            "decisions": ["use REST"],
            "outcome": "Done.",
            "confidence": 0.85,
        }

        # Create a backing memory + closed episode
        mem = Memory(content="episode backing memory", type="fact")
        await store.save_memory(mem)
        ep_node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.EPISODE,
            metadata={
                "status": "closed",
                "episode_title": "API Migration",
                "topic_entities": ["api", "auth"],
                "member_count": 2,
            },
        )
        await store.save_memory_node(ep_node)

        # Create members
        for i in range(2):
            m = Memory(content=f"fragment {i}", type="fact", domains=["backend"])
            await store.save_memory(m)
            member = MemoryNode(
                memory_id=m.id,
                node_type=NodeType.ATOMIC,
                parent_id=ep_node.id,
            )
            await store.save_memory_node(member)

        config = _episode_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        count = await svc.consolidate_episodes()

        assert count == 1

        # Verify abstract node created
        abstracts = await store.get_abstract_nodes_by_type("episode_summary")
        assert len(abstracts) == 1
        assert abstracts[0].metadata["source_episode_id"] == ep_node.id

        # Verify episode marked as summarized
        updated_ep = await store.get_memory_node(ep_node.id)
        assert updated_ep is not None
        assert updated_ep.metadata.get("summarized") is True

        # Verify graph edges
        edges = await store.get_graph_edges(abstracts[0].id, EdgeType.SUMMARIZES.value)
        assert len(edges) == 1
        assert edges[0].target_id == ep_node.id

        derived = await store.get_graph_edges(abstracts[0].id, EdgeType.DERIVED_FROM.value)
        assert len(derived) == 2

    @patch(
        "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
        new_callable=AsyncMock,
    )
    async def test_idempotent_skips_summarized(self, mock_llm, store, index) -> None:
        """Already summarized episodes should be skipped."""
        mem = Memory(content="ep backing", type="fact")
        await store.save_memory(mem)
        ep_node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.EPISODE,
            metadata={"status": "closed", "summarized": True},
        )
        await store.save_memory_node(ep_node)

        config = _episode_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        count = await svc.consolidate_episodes()
        assert count == 0
        mock_llm.assert_not_called()

    @patch(
        "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
        new_callable=AsyncMock,
    )
    async def test_llm_failure_skips_episode(self, mock_llm, store, index) -> None:
        """LLM failure should skip episode, not crash."""
        mock_llm.side_effect = RuntimeError("LLM error")

        mem = Memory(content="ep backing", type="fact")
        await store.save_memory(mem)
        ep_node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.EPISODE,
            metadata={"status": "closed", "episode_title": "Test"},
        )
        await store.save_memory_node(ep_node)

        # Add a member
        m = Memory(content="fragment", type="fact")
        await store.save_memory(m)
        member = MemoryNode(
            memory_id=m.id, node_type=NodeType.ATOMIC, parent_id=ep_node.id,
        )
        await store.save_memory_node(member)

        config = _episode_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        count = await svc.consolidate_episodes()
        assert count == 0


# ── Phase 5B: Trajectory Consolidation ──────────────────────────────────


class TestConsolidateTrajectories:
    """Tests for consolidate_trajectories (Phase 5B)."""

    async def test_disabled_returns_zero(self, store, index) -> None:
        config = NCMSConfig(db_path=":memory:", trajectory_consolidation_enabled=False)
        svc = ConsolidationService(store=store, index=index, config=config)
        assert await svc.consolidate_trajectories() == 0

    async def test_no_entities_with_enough_states(self, store, index) -> None:
        config = _trajectory_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        assert await svc.consolidate_trajectories() == 0

    @patch(
        "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
        new_callable=AsyncMock,
    )
    async def test_creates_trajectory_from_state_history(
        self, mock_llm, store, index,
    ) -> None:
        mock_llm.return_value = {
            "narrative": "Version progressed from v1 to v3.",
            "trend": "improving",
            "key_transitions": ["v1→v2", "v2→v3"],
            "confidence": 0.9,
        }

        # Create entity states
        for i in range(4):
            mem = Memory(content=f"state {i}", type="fact")
            await store.save_memory(mem)
            await store.save_memory_node(MemoryNode(
                memory_id=mem.id,
                node_type=NodeType.ENTITY_STATE,
                metadata={
                    "entity_id": "ent-api",
                    "state_key": "version",
                    "state_value": f"v{i}",
                },
            ))

        config = _trajectory_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        count = await svc.consolidate_trajectories()
        assert count == 1

        abstracts = await store.get_abstract_nodes_by_type("state_trajectory")
        assert len(abstracts) == 1
        assert abstracts[0].metadata["entity_id"] == "ent-api"
        assert abstracts[0].metadata["trend"] == "improving"

    @patch(
        "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
        new_callable=AsyncMock,
    )
    async def test_skips_entity_with_existing_trajectory(
        self, mock_llm, store, index,
    ) -> None:
        """Entity with an existing non-stale trajectory should be skipped."""
        # Create states
        for i in range(4):
            mem = Memory(content=f"state {i}", type="fact")
            await store.save_memory(mem)
            await store.save_memory_node(MemoryNode(
                memory_id=mem.id,
                node_type=NodeType.ENTITY_STATE,
                metadata={"entity_id": "ent-x", "state_key": "k", "state_value": f"v{i}"},
            ))

        # Pre-existing trajectory abstract
        abs_mem = Memory(content="existing trajectory", type="insight")
        await store.save_memory(abs_mem)
        future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        await store.save_memory_node(MemoryNode(
            memory_id=abs_mem.id,
            node_type=NodeType.ABSTRACT,
            metadata={
                "abstract_type": "state_trajectory",
                "entity_id": "ent-x",
                "refresh_due_at": future,
            },
        ))

        config = _trajectory_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        count = await svc.consolidate_trajectories()
        assert count == 0
        mock_llm.assert_not_called()


# ── Phase 5C: Pattern Consolidation ─────────────────────────────────────


class TestConsolidatePatterns:
    """Tests for consolidate_patterns (Phase 5C)."""

    async def test_disabled_returns_zero(self, store, index) -> None:
        config = NCMSConfig(db_path=":memory:", pattern_consolidation_enabled=False)
        svc = ConsolidationService(store=store, index=index, config=config)
        assert await svc.consolidate_patterns() == 0

    async def test_too_few_summaries_returns_zero(self, store, index) -> None:
        config = _pattern_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        assert await svc.consolidate_patterns() == 0

    @patch(
        "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
        new_callable=AsyncMock,
    )
    async def test_creates_pattern_from_similar_episodes(
        self, mock_llm, store, index,
    ) -> None:
        mock_llm.return_value = {
            "pattern": "API migrations follow a common pattern.",
            "pattern_type": "workflow",
            "recurrence_count": 3,
            "confidence": 0.6,
            "key_entities": ["api"],
        }

        # Create 3 episode summary abstracts with overlapping entities
        shared = ["api", "auth", "database"]
        for i in range(3):
            mem = Memory(content=f"Episode {i} summary", type="insight")
            await store.save_memory(mem)
            await store.save_memory_node(MemoryNode(
                memory_id=mem.id,
                node_type=NodeType.ABSTRACT,
                metadata={
                    "abstract_type": "episode_summary",
                    "source_episode_id": f"ep-{i}",
                    "topic_entities": shared,
                },
            ))

        config = _pattern_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        count = await svc.consolidate_patterns()
        assert count == 1

        # With 3 episodes and 0.6 confidence:
        # stability = min(1.0, 3/5) * 0.6 = 0.6 * 0.6 = 0.36
        # Below 0.7 threshold → recurring_pattern
        patterns = await store.get_abstract_nodes_by_type("recurring_pattern")
        assert len(patterns) == 1

    @patch(
        "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
        new_callable=AsyncMock,
    )
    async def test_promotes_to_strategic_insight(
        self, mock_llm, store, index,
    ) -> None:
        """High stability should promote to strategic_insight."""
        mock_llm.return_value = {
            "pattern": "Stable pattern.",
            "pattern_type": "architecture",
            "recurrence_count": 6,
            "confidence": 0.95,
            "key_entities": ["api"],
        }

        shared = ["api", "auth"]
        for i in range(6):
            mem = Memory(content=f"Ep {i} summary", type="insight")
            await store.save_memory(mem)
            await store.save_memory_node(MemoryNode(
                memory_id=mem.id,
                node_type=NodeType.ABSTRACT,
                metadata={
                    "abstract_type": "episode_summary",
                    "source_episode_id": f"ep-{i}",
                    "topic_entities": shared,
                },
            ))

        config = _pattern_config()
        svc = ConsolidationService(store=store, index=index, config=config)
        count = await svc.consolidate_patterns()
        assert count == 1

        # stability = min(1.0, 6/5) * 0.95 = 1.0 * 0.95 = 0.95 >= 0.7
        insights = await store.get_abstract_nodes_by_type("strategic_insight")
        assert len(insights) == 1


# ── Orchestrator ────────────────────────────────────────────────────────


class TestRunConsolidationPass:
    """Tests for run_consolidation_pass orchestrator."""

    async def test_returns_all_subtask_counts(self, store, index) -> None:
        """Even with everything disabled, should return all keys."""
        config = NCMSConfig(
            db_path=":memory:",
            consolidation_knowledge_enabled=False,
            episode_consolidation_enabled=False,
            trajectory_consolidation_enabled=False,
            pattern_consolidation_enabled=False,
        )
        svc = ConsolidationService(store=store, index=index, config=config)
        results = await svc.run_consolidation_pass()

        assert "decay" in results
        assert "knowledge" in results
        assert "episodes" in results
        assert "trajectories" in results
        assert "patterns" in results
        assert "refresh" in results


# ── Staleness Detection ─────────────────────────────────────────────────


class TestIsStale:
    """Tests for _is_stale helper."""

    def test_not_stale_when_future(self, store, index) -> None:
        config = NCMSConfig(db_path=":memory:")
        svc = ConsolidationService(store=store, index=index, config=config)
        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        node = MemoryNode(
            memory_id="m",
            node_type=NodeType.ABSTRACT,
            metadata={"refresh_due_at": future},
        )
        assert svc._is_stale(node) is False

    def test_stale_when_past(self, store, index) -> None:
        config = NCMSConfig(db_path=":memory:")
        svc = ConsolidationService(store=store, index=index, config=config)
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        node = MemoryNode(
            memory_id="m",
            node_type=NodeType.ABSTRACT,
            metadata={"refresh_due_at": past},
        )
        assert svc._is_stale(node) is True

    def test_not_stale_when_no_refresh_field(self, store, index) -> None:
        config = NCMSConfig(db_path=":memory:")
        svc = ConsolidationService(store=store, index=index, config=config)
        node = MemoryNode(
            memory_id="m",
            node_type=NodeType.ABSTRACT,
            metadata={},
        )
        assert svc._is_stale(node) is False


# ── Entity Overlap Clustering ───────────────────────────────────────────


class TestClusterByEntityOverlap:
    """Tests for _cluster_by_entity_overlap."""

    def test_clusters_nodes_with_high_overlap(self, store, index) -> None:
        config = _pattern_config(pattern_entity_overlap_threshold=0.3)
        svc = ConsolidationService(store=store, index=index, config=config)

        nodes = [
            MemoryNode(
                memory_id=f"m{i}",
                node_type=NodeType.ABSTRACT,
                metadata={"topic_entities": ["api", "auth", "db"]},
            )
            for i in range(4)
        ]

        clusters = svc._cluster_by_entity_overlap(nodes)
        assert len(clusters) == 1
        assert len(clusters[0][0]) == 4

    def test_no_clusters_below_min_size(self, store, index) -> None:
        config = _pattern_config(pattern_min_episodes=5)
        svc = ConsolidationService(store=store, index=index, config=config)

        nodes = [
            MemoryNode(
                memory_id=f"m{i}",
                node_type=NodeType.ABSTRACT,
                metadata={"topic_entities": ["api"]},
            )
            for i in range(3)
        ]

        clusters = svc._cluster_by_entity_overlap(nodes)
        assert len(clusters) == 0

    def test_separates_distinct_groups(self, store, index) -> None:
        config = _pattern_config(
            pattern_min_episodes=2, pattern_entity_overlap_threshold=0.5,
        )
        svc = ConsolidationService(store=store, index=index, config=config)

        # Group A: api/auth entities
        # Group B: db/cache entities (no overlap with A → separate clusters)
        nodes = [
            MemoryNode(
                memory_id="a1",
                node_type=NodeType.ABSTRACT,
                metadata={"topic_entities": ["api", "auth"]},
            ),
            MemoryNode(
                memory_id="a2",
                node_type=NodeType.ABSTRACT,
                metadata={"topic_entities": ["api", "auth"]},
            ),
            MemoryNode(
                memory_id="b1",
                node_type=NodeType.ABSTRACT,
                metadata={"topic_entities": ["database", "cache"]},
            ),
            MemoryNode(
                memory_id="b2",
                node_type=NodeType.ABSTRACT,
                metadata={"topic_entities": ["database", "cache"]},
            ),
        ]

        clusters = svc._cluster_by_entity_overlap(nodes)
        assert len(clusters) == 2
