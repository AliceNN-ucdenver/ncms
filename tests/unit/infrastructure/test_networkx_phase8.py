"""Tests for Phase 8 NetworkX graph: PageRank centrality, get_memory_ids_for_entity."""

from __future__ import annotations

import pytest

from ncms.domain.models import Entity, Relationship
from ncms.infrastructure.graph.networkx_store import NetworkXGraph


@pytest.fixture
def graph() -> NetworkXGraph:
    return NetworkXGraph()


class TestPageRank:
    def test_empty_graph_returns_empty(self, graph: NetworkXGraph) -> None:
        result = graph.pagerank()
        assert result == {}

    def test_single_entity(self, graph: NetworkXGraph) -> None:
        graph.add_entity(Entity(id="e1", name="Entity1", type="concept"))
        result = graph.pagerank()
        assert "e1" in result
        assert result["e1"] == pytest.approx(1.0)

    def test_connected_entities(self, graph: NetworkXGraph) -> None:
        graph.add_entity(Entity(id="hub", name="Hub", type="concept"))
        for i in range(3):
            graph.add_entity(Entity(id=f"leaf-{i}", name=f"Leaf{i}", type="concept"))
            graph.add_relationship(
                Relationship(
                    source_entity_id=f"leaf-{i}",
                    target_entity_id="hub",
                    type="depends_on",
                )
            )

        result = graph.pagerank()
        # Hub should have higher centrality than leaves
        assert result["hub"] > result["leaf-0"]
        # All scores should sum to ~1.0
        assert sum(result.values()) == pytest.approx(1.0, abs=0.01)


class TestGetMemoryIdsForEntity:
    def test_no_links_returns_empty(self, graph: NetworkXGraph) -> None:
        result = graph.get_memory_ids_for_entity("nonexistent")
        assert result == set()

    def test_returns_linked_memories(self, graph: NetworkXGraph) -> None:
        graph.link_memory_entity("mem-1", "e1")
        graph.link_memory_entity("mem-2", "e1")
        graph.link_memory_entity("mem-3", "e2")

        result = graph.get_memory_ids_for_entity("e1")
        assert result == {"mem-1", "mem-2"}

    def test_returns_copy_not_reference(self, graph: NetworkXGraph) -> None:
        """Modifying returned set should not affect internal state."""
        graph.link_memory_entity("mem-1", "e1")

        result = graph.get_memory_ids_for_entity("e1")
        result.add("mem-extra")

        # Internal state should be unaffected
        assert graph.get_memory_ids_for_entity("e1") == {"mem-1"}
