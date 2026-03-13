"""Integration tests: hybrid episode linker pipeline end-to-end."""

from __future__ import annotations

import pytest
import pytest_asyncio

from ncms.application.episode_service import EpisodeService
from ncms.config import NCMSConfig
from ncms.domain.models import (
    AccessRecord,
    EdgeType,
    Entity,
    EpisodeMeta,
    EpisodeStatus,
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
async def episode_svc(store, index, config):
    return EpisodeService(store=store, index=index, config=config, splade=None)


async def _save_fragment(
    store: SQLiteStore,
    index: TantivyEngine,
    content: str,
    entity_names: list[str],
    *,
    domains: list[str] | None = None,
    source_agent: str | None = None,
) -> tuple[Memory, MemoryNode, list[str]]:
    """Create a Memory + ATOMIC MemoryNode + entities + index + access log."""
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


class TestEpisodeLifecycle:
    """Full pipeline: store fragments → episode creation → assignment → closure."""

    async def test_three_fragments_same_entities(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """Three fragments sharing entities → one episode with 3 members."""
        # Fragment 1: creates episode
        mem1, node1, eids1 = await _save_fragment(
            store, index, "Auth service deployment to staging",
            ["auth-service", "deployment"],
            domains=["api"], source_agent="agent-alpha",
        )
        ep = await episode_svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep is not None
        assert ep.node_type == NodeType.EPISODE

        # Fragment 2: joins episode (shared entities + domain + agent)
        mem2, node2, eids2 = await _save_fragment(
            store, index, "Auth service endpoint changes ready for review",
            ["auth-service", "endpoint"],
            domains=["api"], source_agent="agent-alpha",
        )
        ep2 = await episode_svc.assign_or_create(node2, mem2, entity_ids=eids2)
        assert ep2 is not None
        assert ep2.id == ep.id

        # Fragment 3: also joins
        mem3, node3, eids3 = await _save_fragment(
            store, index, "Auth service deployed to production",
            ["auth-service", "production"],
            domains=["api"], source_agent="agent-alpha",
        )
        ep3 = await episode_svc.assign_or_create(node3, mem3, entity_ids=eids3)
        assert ep3 is not None
        assert ep3.id == ep.id

        # Verify episode metadata
        refreshed = await store.get_memory_node(ep.id)
        assert refreshed is not None
        ep_meta = EpisodeMeta.from_node(refreshed)
        assert ep_meta is not None
        assert ep_meta.member_count == 3
        assert "auth-service" in ep_meta.topic_entities

        # Verify members via parent_id query
        members = await store.get_episode_members(ep.id)
        assert len(members) == 3

        # Verify BELONGS_TO_EPISODE edges
        for member in members:
            edges = await store.get_graph_edges(
                member.id, EdgeType.BELONGS_TO_EPISODE,
            )
            assert len(edges) == 1
            assert edges[0].target_id == ep.id

    async def test_episode_closure_on_resolution(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """Fragment with 'resolved' closes the episode."""
        mem1, node1, eids1 = await _save_fragment(
            store, index, "Investigating payment service outage",
            ["payment-service", "outage"],
            domains=["payments"], source_agent="agent-beta",
        )
        ep = await episode_svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep is not None

        # Check resolution on a fragment that says "resolved"
        closed = await episode_svc.check_resolution_closure(
            "The payment outage has been resolved", ep,
        )
        assert closed is True

        refreshed = await store.get_memory_node(ep.id)
        assert refreshed is not None
        assert refreshed.metadata["status"] == "closed"

    async def test_stale_episode_closure(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
    ) -> None:
        """Episode with no recent members gets auto-closed."""
        short_config = NCMSConfig(
            db_path=":memory:",
            episodes_enabled=True,
            episode_close_minutes=0,
            episode_create_min_entities=2,
        )
        svc = EpisodeService(store=store, index=index, config=short_config)

        mem1, node1, eids1 = await _save_fragment(
            store, index, "Database failover triggered by load spike",
            ["database", "failover"],
            domains=["db"],
        )
        ep = await svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep is not None

        closed_ids = await svc.close_stale_episodes()
        assert ep.id in closed_ids

        refreshed = await store.get_memory_node(ep.id)
        assert refreshed is not None
        assert refreshed.metadata["status"] == "closed"

    async def test_entity_overlap_joins_episode(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """Fragment joins episode via shared entities (primary matching path)."""
        # Create episode from first fragment
        mem1, node1, eids1 = await _save_fragment(
            store, index, "PROJ-200 auth service deployment started",
            ["auth-service", "deployment"],
            domains=["api"], source_agent="agent-alpha",
        )
        entity = Entity(name="auth-service", type="service")
        await store.save_entity(entity)
        await store.link_memory_entity(mem1.id, entity.id)

        ep = await episode_svc.assign_or_create(
            node1, mem1, entity_ids=eids1,
        )
        assert ep is not None

        # Second fragment: shared entity + domain + agent
        mem2, node2, eids2 = await _save_fragment(
            store, index, "Auth service health check passed after changes",
            ["auth-service", "health-check"],
            domains=["api"], source_agent="agent-alpha",
        )
        ep2 = await episode_svc.assign_or_create(
            node2, mem2, entity_ids=eids2,
        )
        assert ep2 is not None
        assert ep2.id == ep.id

    async def test_episode_profile_searchable_via_bm25(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """Episode's backing Memory (profile) is indexed and searchable."""
        mem1, node1, eids1 = await _save_fragment(
            store, index, "Working on payment refactor project",
            ["payment-service", "refactor"],
            domains=["payments"],
        )
        ep = await episode_svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep is not None

        # Search for the episode profile via BM25
        results = index.search("payment refactor", limit=5)
        memory_ids = [mid for mid, _ in results]

        # The episode's backing memory should be in results
        assert ep.memory_id in memory_ids

    async def test_different_clusters_create_separate_episodes(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """Fragments with different entity clusters create separate episodes."""
        mem1, node1, eids1 = await _save_fragment(
            store, index, "Frontend React component refactoring",
            ["frontend", "react"],
            domains=["frontend"],
        )
        ep1 = await episode_svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep1 is not None

        mem2, node2, eids2 = await _save_fragment(
            store, index, "Backend database migration to PostgreSQL 16",
            ["backend", "postgresql"],
            domains=["backend"],
        )
        ep2 = await episode_svc.assign_or_create(node2, mem2, entity_ids=eids2)
        assert ep2 is not None

        # Should be different episodes
        assert ep1.id != ep2.id

        # Verify both are open
        open_eps = await store.get_open_episodes()
        open_ids = {ep.id for ep in open_eps}
        assert ep1.id in open_ids
        assert ep2.id in open_ids


class TestCrossDomainEpisodes:
    """Verify episodes form across different content domains."""

    async def test_scientific_domain_episode(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """Scientific fragments about gene editing form an episode."""
        mem1, node1, eids1 = await _save_fragment(
            store, index,
            "CRISPR-Cas9 gene editing shows promising results in trials",
            ["CRISPR-Cas9", "gene-editing"],
            domains=["biology"],
        )
        ep = await episode_svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep is not None

        meta = EpisodeMeta.from_node(ep)
        assert meta is not None
        assert meta.anchor_type == "entity_cluster"
        assert "CRISPR-Cas9" in meta.topic_entities

        # Related scientific fragment joins
        mem2, node2, eids2 = await _save_fragment(
            store, index,
            "New CRISPR-Cas9 delivery mechanism improves efficiency",
            ["CRISPR-Cas9", "delivery-mechanism"],
            domains=["biology"],
        )
        ep2 = await episode_svc.assign_or_create(node2, mem2, entity_ids=eids2)
        assert ep2 is not None
        assert ep2.id == ep.id  # Joined same episode

    async def test_ticket_domain_episode(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """Software ticket fragments form structured episode."""
        mem1, node1, eids1 = await _save_fragment(
            store, index,
            "Starting work on JIRA-500 payment refactor",
            ["payment-service", "JIRA-500"],
            domains=["payments"],
        )
        ep = await episode_svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep is not None

        meta = EpisodeMeta.from_node(ep)
        assert meta is not None
        assert meta.anchor_type == "structured:issue_id"
        assert meta.anchor_id == "JIRA-500"

        # Related ticket fragment joins
        mem2, node2, eids2 = await _save_fragment(
            store, index,
            "JIRA-500 payment refactor code review complete",
            ["payment-service", "code-review"],
            domains=["payments"],
        )
        ep2 = await episode_svc.assign_or_create(node2, mem2, entity_ids=eids2)
        assert ep2 is not None
        assert ep2.id == ep.id

    async def test_general_prose_episode(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """General prose fragments about same topic form an episode."""
        mem1, node1, eids1 = await _save_fragment(
            store, index,
            "Supply chain disruption affecting semiconductor availability",
            ["supply-chain", "semiconductor"],
            domains=["logistics"],
        )
        ep = await episode_svc.assign_or_create(node1, mem1, entity_ids=eids1)
        assert ep is not None

        meta = EpisodeMeta.from_node(ep)
        assert meta is not None
        assert meta.anchor_type == "entity_cluster"

        # Related fragment about same topic
        mem2, node2, eids2 = await _save_fragment(
            store, index,
            "Semiconductor shortage impacts production schedule",
            ["semiconductor", "production"],
            domains=["logistics"],
        )
        ep2 = await episode_svc.assign_or_create(node2, mem2, entity_ids=eids2)
        assert ep2 is not None
        assert ep2.id == ep.id  # Same topic → same episode

    async def test_cross_domain_isolation(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        episode_svc: EpisodeService,
    ) -> None:
        """Scientific, ticket, and general episodes stay separate."""
        # Scientific episode
        mem1, node1, eids1 = await _save_fragment(
            store, index,
            "Protein folding simulation using AlphaFold",
            ["protein-folding", "AlphaFold"],
            domains=["biology"],
        )
        ep_sci = await episode_svc.assign_or_create(node1, mem1, entity_ids=eids1)

        # Ticket episode
        mem2, node2, eids2 = await _save_fragment(
            store, index,
            "PROJ-123 user authentication redesign",
            ["authentication", "PROJ-123"],
            domains=["api"],
        )
        ep_ticket = await episode_svc.assign_or_create(node2, mem2, entity_ids=eids2)

        # General episode
        mem3, node3, eids3 = await _save_fragment(
            store, index,
            "Quarterly budget review for marketing department",
            ["budget-review", "marketing"],
            domains=["finance"],
        )
        ep_general = await episode_svc.assign_or_create(node3, mem3, entity_ids=eids3)

        # All three should be separate episodes
        assert ep_sci is not None
        assert ep_ticket is not None
        assert ep_general is not None
        assert len({ep_sci.id, ep_ticket.id, ep_general.id}) == 3
