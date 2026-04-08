"""Unit tests: hybrid episode linker (Phase 3 rework).

Tests entity-based topic matching, BM25/SPLADE candidate generation,
weighted multi-signal scoring, and episode lifecycle.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from ncms.application.episode_service import EpisodeService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    AccessRecord,
    Entity,
    EpisodeMeta,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def index() -> TantivyEngine:
    engine = TantivyEngine()
    engine.initialize()
    return engine


@pytest.fixture
def config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        episodes_enabled=True,
        episode_window_minutes=1440,
        episode_close_minutes=1440,
        episode_create_min_entities=2,
        episode_match_threshold=0.30,
    )


@pytest_asyncio.fixture
async def svc(store, index, config):
    return EpisodeService(store=store, index=index, config=config, splade=None)


async def _create_memory_with_entities(
    store: SQLiteStore,
    index: TantivyEngine,
    content: str,
    entity_names: list[str],
    *,
    domains: list[str] | None = None,
    source_agent: str | None = None,
) -> tuple[Memory, MemoryNode, list[str]]:
    """Helper: create Memory + MemoryNode + entities + link them."""
    mem = Memory(
        content=content,
        domains=domains or ["test"],
        source_agent=source_agent,
    )
    await store.save_memory(mem)
    index.index_memory(mem)
    await store.log_access(AccessRecord(memory_id=mem.id))

    node = MemoryNode(
        memory_id=mem.id,
        node_type=NodeType.ATOMIC,
        importance=5.0,
    )
    await store.save_memory_node(node)

    entity_ids: list[str] = []
    for name in entity_names:
        entity = Entity(name=name, type="concept")
        await store.save_entity(entity)
        await store.link_memory_entity(mem.id, entity.id)
        entity_ids.append(entity.id)

    return mem, node, entity_ids


# ── Entity Overlap Tests ─────────────────────────────────────────────────


class TestEntityOverlap:
    """Test the overlap coefficient for entity matching."""

    def test_full_overlap(self) -> None:
        """Fragment entities fully contained in episode."""
        result = EpisodeService.compute_entity_overlap(
            ["a", "b"], ["a", "b", "c", "d"],
        )
        assert result == 1.0  # |{a,b}| / min(2, 4) = 2/2

    def test_partial_overlap(self) -> None:
        """Some entities shared."""
        result = EpisodeService.compute_entity_overlap(
            ["a", "b", "c"], ["a", "d", "e"],
        )
        assert abs(result - 1 / 3) < 0.01  # |{a}| / min(3, 3) = 1/3

    def test_no_overlap(self) -> None:
        """No shared entities → 0.0."""
        result = EpisodeService.compute_entity_overlap(["a"], ["b"])
        assert result == 0.0

    def test_empty_fragment(self) -> None:
        """Empty fragment entities → 0.0."""
        result = EpisodeService.compute_entity_overlap([], ["a", "b"])
        assert result == 0.0

    def test_empty_episode(self) -> None:
        """Empty episode entities → 0.0."""
        result = EpisodeService.compute_entity_overlap(["a"], [])
        assert result == 0.0

    def test_asymmetric_favors_small_set(self) -> None:
        """Small fragment matching one entity in large episode → 1.0."""
        result = EpisodeService.compute_entity_overlap(
            ["x"], ["x", "y", "z", "w", "v"],
        )
        assert result == 1.0


# ── Structured Anchor Detection Tests ─────────────────────────────────────


class TestDetectAnchor:
    """Structured anchor detection (bonus signal, not primary)."""

    def test_jira_issue_id(self) -> None:
        result = EpisodeService.detect_anchor("Working on JIRA-123 auth refactor")
        assert result is not None
        assert result[0] == "issue_id"
        assert result[1] == "JIRA-123"

    def test_github_pr(self) -> None:
        result = EpisodeService.detect_anchor("PR-789 is ready for review")
        assert result is not None
        assert result[0] == "issue_id"

    def test_incident_marker(self) -> None:
        result = EpisodeService.detect_anchor("Payment service outage detected")
        assert result is not None
        assert result[0] == "incident"

    def test_no_anchor(self) -> None:
        result = EpisodeService.detect_anchor("The auth service is working fine")
        assert result is None


# ── Assignment Pipeline Tests ─────────────────────────────────────────────


class TestAssignOrCreate:
    """Test the full assign_or_create pipeline with entity-based matching."""

    async def test_creates_episode_from_entity_cluster(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Fragment with >= min_entities and no matching episode → new episode."""
        mem, node, eids = await _create_memory_with_entities(
            store, index,
            "Auth service deployment to staging environment",
            ["auth-service", "staging"],
            domains=["api"],
        )
        ep = await svc.assign_or_create(node, mem, entity_ids=eids)

        assert ep is not None
        assert ep.node_type == NodeType.EPISODE
        meta = EpisodeMeta.from_node(ep)
        assert meta is not None
        assert meta.anchor_type == "entity_cluster"
        assert "auth-service" in meta.topic_entities
        assert "staging" in meta.topic_entities
        assert meta.member_count == 1

    async def test_creates_episode_with_structured_anchor(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Fragment with JIRA ID + entities → structured episode."""
        mem, node, eids = await _create_memory_with_entities(
            store, index,
            "Starting work on JIRA-100 auth refactor",
            ["auth-service", "JIRA-100"],
            domains=["api"],
        )
        ep = await svc.assign_or_create(node, mem, entity_ids=eids)

        assert ep is not None
        meta = EpisodeMeta.from_node(ep)
        assert meta is not None
        assert meta.anchor_type == "structured:issue_id"
        assert meta.anchor_id == "JIRA-100"

    async def test_entity_overlap_joins_existing_episode(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Fragment sharing entities with episode → joins via entity overlap."""
        # Create first fragment → new episode
        mem1, node1, eids1 = await _create_memory_with_entities(
            store, index,
            "Auth service deployment started",
            ["auth-service", "deployment"],
            domains=["api"], source_agent="agent-alpha",
        )
        ep1 = await svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep1 is not None

        # Second fragment shares entities → should join
        mem2, node2, eids2 = await _create_memory_with_entities(
            store, index,
            "Auth service health check passed after changes",
            ["auth-service", "health-check"],
            domains=["api"], source_agent="agent-alpha",
        )
        ep2 = await svc.assign_or_create(node2, mem2, entity_ids=eids2)

        assert ep2 is not None
        assert ep2.id == ep1.id  # Joined same episode

    async def test_insufficient_entities_returns_none(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Fragment with < min_entities and no matching episode → None."""
        mem, node, eids = await _create_memory_with_entities(
            store, index,
            "Quick note about something",
            ["something"],  # Only 1 entity < min_entities=2
        )
        ep = await svc.assign_or_create(node, mem, entity_ids=eids)
        assert ep is None

    async def test_no_entities_returns_none(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Fragment with zero entities → None."""
        mem = Memory(content="A simple note", domains=["test"])
        await store.save_memory(mem)
        index.index_memory(mem)
        node = MemoryNode(
            memory_id=mem.id, node_type=NodeType.ATOMIC, importance=5.0,
        )
        await store.save_memory_node(node)

        ep = await svc.assign_or_create(node, mem, entity_ids=[])
        assert ep is None

    async def test_best_matching_episode_wins(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """When multiple episodes match, highest score wins."""
        # Create two episodes with different entity clusters
        mem1, node1, eids1 = await _create_memory_with_entities(
            store, index,
            "Frontend React components updated",
            ["frontend", "react"],
            domains=["frontend"],
        )
        ep_frontend = await svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep_frontend is not None

        mem2, node2, eids2 = await _create_memory_with_entities(
            store, index,
            "Backend PostgreSQL migration completed",
            ["backend", "postgresql"],
            domains=["backend"],
        )
        ep_backend = await svc.assign_or_create(node2, mem2, entity_ids=eids2)
        assert ep_backend is not None
        assert ep_frontend.id != ep_backend.id

        # Fragment about frontend+react → should join frontend episode
        mem3, node3, eids3 = await _create_memory_with_entities(
            store, index,
            "React component testing complete",
            ["frontend", "react"],
            domains=["frontend"],
        )
        ep3 = await svc.assign_or_create(node3, mem3, entity_ids=eids3)
        assert ep3 is not None
        assert ep3.id == ep_frontend.id

    async def test_different_entity_clusters_create_separate_episodes(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Fragments with different entities → separate episodes."""
        mem1, node1, eids1 = await _create_memory_with_entities(
            store, index,
            "Payment gateway integration started",
            ["payment-gateway", "stripe"],
            domains=["payments"],
        )
        ep1 = await svc.assign_or_create(node1, mem1, entity_ids=eids1)

        mem2, node2, eids2 = await _create_memory_with_entities(
            store, index,
            "User analytics dashboard redesign",
            ["analytics", "dashboard"],
            domains=["frontend"],
        )
        ep2 = await svc.assign_or_create(node2, mem2, entity_ids=eids2)

        assert ep1 is not None
        assert ep2 is not None
        assert ep1.id != ep2.id


# ── Episode Profile Tests ────────────────────────────────────────────────


class TestEpisodeProfile:
    """Test episode profile management."""

    async def test_profile_contains_entities(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Episode profile content includes member entity names."""
        mem, node, eids = await _create_memory_with_entities(
            store, index,
            "Auth service deployment started",
            ["auth-service", "deployment"],
            domains=["api"],
        )
        ep = await svc.assign_or_create(node, mem, entity_ids=eids)
        assert ep is not None

        # Check the backing memory profile
        ep_memory = await store.get_memory(ep.memory_id)
        assert ep_memory is not None
        assert "auth-service" in ep_memory.content
        assert "deployment" in ep_memory.content

    async def test_profile_searchable_via_bm25(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Episode profile is BM25-searchable."""
        mem, node, eids = await _create_memory_with_entities(
            store, index,
            "PostgreSQL migration to version 15",
            ["postgresql", "migration"],
            domains=["database"],
        )
        ep = await svc.assign_or_create(node, mem, entity_ids=eids)
        assert ep is not None

        # BM25 search should find the episode's backing memory
        results = index.search("postgresql migration", limit=5)
        memory_ids = [mid for mid, _ in results]
        assert ep.memory_id in memory_ids

    async def test_profile_enriched_on_member_join(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """New member's unique entities are added to episode profile."""
        # First fragment
        mem1, node1, eids1 = await _create_memory_with_entities(
            store, index,
            "Auth service deployment started",
            ["auth-service", "deployment"],
            domains=["api"], source_agent="agent-alpha",
        )
        ep = await svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep is not None

        # Second fragment with a new entity
        mem2, node2, eids2 = await _create_memory_with_entities(
            store, index,
            "Auth service JWT validation updated",
            ["auth-service", "jwt"],
            domains=["api"], source_agent="agent-alpha",
        )
        ep2 = await svc.assign_or_create(node2, mem2, entity_ids=eids2)
        assert ep2 is not None
        assert ep2.id == ep.id

        # Episode should now have "jwt" in topic_entities
        refreshed = await store.get_memory_node(ep.id)
        assert refreshed is not None
        topic_entities = refreshed.metadata.get("topic_entities", [])
        assert "jwt" in topic_entities


# ── Episode Closure Tests ─────────────────────────────────────────────────


class TestEpisodeClosure:
    """Test episode closure (stale timeout + resolution markers)."""

    async def test_stale_episode_closure(
        self, store: SQLiteStore, index: TantivyEngine,
    ) -> None:
        """Episode with no recent members gets auto-closed."""
        short_config = NCMSConfig(
            db_path=":memory:",
            episodes_enabled=True,
            episode_close_minutes=0,  # Close immediately
            episode_create_min_entities=2,
        )
        svc = EpisodeService(store=store, index=index, config=short_config)

        mem, node, eids = await _create_memory_with_entities(
            store, index,
            "Database failover triggered",
            ["database", "failover"],
            domains=["db"],
        )
        ep = await svc.assign_or_create(node, mem, entity_ids=eids)
        assert ep is not None

        closed_ids = await svc.close_stale_episodes()
        assert ep.id in closed_ids

        refreshed = await store.get_memory_node(ep.id)
        assert refreshed is not None
        assert refreshed.metadata["status"] == "closed"

    async def test_resolution_closure(
        self, store: SQLiteStore, index: TantivyEngine, svc: EpisodeService,
    ) -> None:
        """Resolution marker in fragment closes the episode."""
        mem, node, eids = await _create_memory_with_entities(
            store, index,
            "Investigating payment outage",
            ["payment-service", "outage"],
            domains=["payments"],
        )
        ep = await svc.assign_or_create(node, mem, entity_ids=eids)
        assert ep is not None

        closed = await svc.check_resolution_closure(
            "The payment outage has been resolved", ep,
        )
        assert closed is True

        refreshed = await store.get_memory_node(ep.id)
        assert refreshed is not None
        assert refreshed.metadata["status"] == "closed"


# ── EpisodeMeta Tests ─────────────────────────────────────────────────────


class TestEpisodeMeta:
    """Test EpisodeMeta validation and extraction."""

    def test_topic_entities_round_trip(self) -> None:
        """topic_entities survives metadata → EpisodeMeta conversion."""
        node = MemoryNode(
            memory_id="m1",
            node_type=NodeType.EPISODE,
            metadata={
                "episode_title": "Episode: auth, deploy [api]",
                "anchor_type": "entity_cluster",
                "anchor_id": "auth+deploy",
                "member_count": 3,
                "topic_entities": ["auth-service", "deploy", "jwt"],
            },
        )
        meta = EpisodeMeta.from_node(node)
        assert meta is not None
        assert meta.topic_entities == ["auth-service", "deploy", "jwt"]
        assert meta.anchor_type == "entity_cluster"

    def test_backward_compat_no_topic_entities(self) -> None:
        """Old episodes without topic_entities default to empty list."""
        node = MemoryNode(
            memory_id="m1",
            node_type=NodeType.EPISODE,
            metadata={
                "episode_title": "Episode: issue_id JIRA-100",
                "anchor_type": "issue_id",
                "anchor_id": "JIRA-100",
                "member_count": 2,
            },
        )
        meta = EpisodeMeta.from_node(node)
        assert meta is not None
        assert meta.topic_entities == []

    def test_missing_title_returns_none(self) -> None:
        """Missing episode_title → None."""
        node = MemoryNode(
            memory_id="m1",
            node_type=NodeType.EPISODE,
            metadata={"anchor_type": "entity_cluster"},
        )
        assert EpisodeMeta.from_node(node) is None

    def test_missing_anchor_type_returns_none(self) -> None:
        """Missing anchor_type → None."""
        node = MemoryNode(
            memory_id="m1",
            node_type=NodeType.EPISODE,
            metadata={"episode_title": "Episode: test"},
        )
        assert EpisodeMeta.from_node(node) is None


# ── Build Profile Content Tests ──────────────────────────────────────────


class TestBuildProfileContent:
    """Test episode profile content generation."""

    def test_entities_and_domains(self) -> None:
        result = EpisodeService._build_profile_content(
            ["auth-service", "jwt"], ["api"], {},
        )
        assert "auth-service" in result
        assert "jwt" in result
        assert "api" in result

    def test_with_anchor(self) -> None:
        result = EpisodeService._build_profile_content(
            ["auth"], ["api"], {"anchor_id": "JIRA-100"},
        )
        assert "JIRA-100" in result

    def test_empty(self) -> None:
        result = EpisodeService._build_profile_content([], [], {})
        assert result == "episode"
