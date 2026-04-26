"""Integration tests for Phase 5 consolidation pipeline.

Tests the full flow: store memories → create episodes → close →
consolidate → verify abstracts are searchable.
"""

from __future__ import annotations

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


@patch(
    "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
    new_callable=AsyncMock,
)
async def test_episode_summary_searchable_after_consolidation(
    mock_llm,
    store,
    index,
) -> None:
    """End-to-end: closed episode → consolidate → summary searchable via BM25."""
    mock_llm.return_value = {
        "summary": "The database schema was migrated from v3 to v4.",
        "actors": ["db-team"],
        "artifacts": ["schema-v4"],
        "decisions": ["add indexes"],
        "outcome": "Migration successful.",
        "confidence": 0.9,
    }

    # Store backing memory + index it
    ep_mem = Memory(content="Episode: database migration", type="fact")
    await store.save_memory(ep_mem)
    index.index_memory(ep_mem)

    # Create closed episode
    ep_node = MemoryNode(
        memory_id=ep_mem.id,
        node_type=NodeType.EPISODE,
        metadata={
            "status": "closed",
            "episode_title": "Database Migration",
            "topic_entities": ["database", "schema"],
        },
    )
    await store.save_memory_node(ep_node)

    # Create members
    for content in ["ALTER TABLE users ADD COLUMN", "CREATE INDEX idx_users_email"]:
        m = Memory(content=content, type="fact", domains=["database"])
        await store.save_memory(m)
        index.index_memory(m)
        member = MemoryNode(
            memory_id=m.id,
            node_type=NodeType.ATOMIC,
            parent_id=ep_node.id,
        )
        await store.save_memory_node(member)

    # Run consolidation
    config = NCMSConfig(
        db_path=":memory:",
        episode_consolidation_enabled=True,
        consolidation_knowledge_model="test-model",
        consolidation_knowledge_api_base=None,
    )
    svc = ConsolidationService(store=store, index=index, config=config)
    count = await svc.consolidate_episodes()
    assert count == 1

    # Verify summary is searchable
    results = index.search("database schema migration", limit=5)
    memory_ids = [r[0] for r in results]
    # The abstract memory should appear in search results
    abstracts = await store.get_abstract_nodes_by_type("episode_summary")
    assert len(abstracts) == 1
    assert abstracts[0].metadata["source_episode_id"] == ep_node.id
    # The backing memory of the abstract should be indexed
    assert abstracts[0].memory_id in memory_ids


@patch(
    "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
    new_callable=AsyncMock,
)
async def test_summarizes_and_derived_from_edges_present(
    mock_llm,
    store,
    index,
) -> None:
    """Verify SUMMARIZES and DERIVED_FROM edges are correctly created."""
    mock_llm.return_value = {
        "summary": "Auth tokens migrated.",
        "actors": ["security"],
        "artifacts": ["tokens"],
        "decisions": ["use JWT"],
        "outcome": "Done.",
        "confidence": 0.8,
    }

    ep_mem = Memory(content="Auth episode", type="fact")
    await store.save_memory(ep_mem)
    ep_node = MemoryNode(
        memory_id=ep_mem.id,
        node_type=NodeType.EPISODE,
        metadata={"status": "closed", "episode_title": "Auth", "topic_entities": []},
    )
    await store.save_memory_node(ep_node)

    members = []
    for i in range(3):
        m = Memory(content=f"auth fragment {i}", type="fact")
        await store.save_memory(m)
        member = MemoryNode(
            memory_id=m.id,
            node_type=NodeType.ATOMIC,
            parent_id=ep_node.id,
        )
        await store.save_memory_node(member)
        members.append(member)

    config = NCMSConfig(
        db_path=":memory:",
        episode_consolidation_enabled=True,
        consolidation_knowledge_model="test-model",
        consolidation_knowledge_api_base=None,
    )
    svc = ConsolidationService(store=store, index=index, config=config)
    await svc.consolidate_episodes()

    abstracts = await store.get_abstract_nodes_by_type("episode_summary")
    assert len(abstracts) == 1
    abstract_id = abstracts[0].id

    # SUMMARIZES edge: abstract → episode
    summarizes = await store.get_graph_edges(abstract_id, EdgeType.SUMMARIZES.value)
    assert len(summarizes) == 1
    assert summarizes[0].target_id == ep_node.id

    # DERIVED_FROM edges: abstract → each member
    derived = await store.get_graph_edges(abstract_id, EdgeType.DERIVED_FROM.value)
    assert len(derived) == 3
    derived_targets = {e.target_id for e in derived}
    for member in members:
        assert member.id in derived_targets


@patch(
    "ncms.infrastructure.consolidation.abstract_synthesizer.call_llm_json",
    new_callable=AsyncMock,
)
async def test_full_consolidation_pass_runs_all_subtasks(
    mock_llm,
    store,
    index,
) -> None:
    """run_consolidation_pass runs all subtasks and returns counts dict."""
    config = NCMSConfig(
        db_path=":memory:",
        consolidation_knowledge_enabled=False,
        episode_consolidation_enabled=False,
        trajectory_consolidation_enabled=False,
        pattern_consolidation_enabled=False,
    )
    svc = ConsolidationService(store=store, index=index, config=config)
    results = await svc.run_consolidation_pass()

    assert isinstance(results, dict)
    expected_keys = {"decay", "knowledge", "episodes", "trajectories", "patterns", "refresh"}
    assert set(results.keys()) == expected_keys
    # All should be 0 when disabled
    assert all(v == 0 for v in results.values())
