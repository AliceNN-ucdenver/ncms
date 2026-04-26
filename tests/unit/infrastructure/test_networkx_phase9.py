"""Tests for Phase 9 NetworkX: Personalized PageRank."""

from __future__ import annotations

import pytest

from ncms.domain.models import Entity, Relationship
from ncms.infrastructure.graph.networkx_store import NetworkXGraph


@pytest.fixture
def graph() -> NetworkXGraph:
    return NetworkXGraph()


class TestPersonalizedPageRank:
    def test_empty_graph_returns_empty(self, graph: NetworkXGraph) -> None:
        result = graph.personalized_pagerank({"e1": 1.0})
        assert result == {}

    def test_empty_seeds_returns_empty(self, graph: NetworkXGraph) -> None:
        graph.add_entity(Entity(id="e1", name="Entity1", type="concept"))
        result = graph.personalized_pagerank({})
        assert result == {}

    def test_seeds_not_in_graph(self, graph: NetworkXGraph) -> None:
        graph.add_entity(Entity(id="e1", name="Entity1", type="concept"))
        result = graph.personalized_pagerank({"nonexistent": 1.0})
        assert result == {}

    def test_single_seed_single_node(self, graph: NetworkXGraph) -> None:
        graph.add_entity(Entity(id="e1", name="Entity1", type="concept"))
        result = graph.personalized_pagerank({"e1": 1.0})
        assert "e1" in result
        assert result["e1"] == pytest.approx(1.0)

    def test_seed_gets_high_score(self, graph: NetworkXGraph) -> None:
        """Seed entity should get higher PPR than distant nodes."""
        graph.add_entity(Entity(id="e1", name="Seed", type="concept"))
        graph.add_entity(Entity(id="e2", name="Neighbor", type="concept"))
        graph.add_entity(Entity(id="e3", name="Distant", type="concept"))

        graph.add_relationship(
            Relationship(
                source_entity_id="e1",
                target_entity_id="e2",
                type="related_to",
            )
        )
        graph.add_relationship(
            Relationship(
                source_entity_id="e2",
                target_entity_id="e3",
                type="related_to",
            )
        )

        result = graph.personalized_pagerank({"e1": 1.0})

        # Seed should have highest score
        assert result["e1"] > result["e2"]
        # Neighbor should rank above distant
        assert result["e2"] > result["e3"]

    def test_multiple_seeds(self, graph: NetworkXGraph) -> None:
        """Multiple seeds should spread activation."""
        for i in range(5):
            graph.add_entity(Entity(id=f"e{i}", name=f"Entity{i}", type="concept"))

        # Star topology: e0 is hub
        for i in range(1, 5):
            graph.add_relationship(
                Relationship(
                    source_entity_id=f"e{i}",
                    target_entity_id="e0",
                    type="related_to",
                )
            )

        # Seed with e1 and e2, both point to e0
        result = graph.personalized_pagerank({"e1": 1.0, "e2": 1.0})

        # e0 (hub receiving from seeds) should get significant score
        assert result["e0"] > 0.01
        # Non-seed, non-neighbor nodes should get less
        assert result["e0"] > result.get("e3", 0)

    def test_idf_weighted_seeds(self, graph: NetworkXGraph) -> None:
        """Higher-IDF seeds should contribute more."""
        graph.add_entity(Entity(id="e1", name="Rare", type="concept"))
        graph.add_entity(Entity(id="e2", name="Common", type="concept"))
        graph.add_entity(Entity(id="e3", name="Target", type="concept"))

        graph.add_relationship(
            Relationship(
                source_entity_id="e1",
                target_entity_id="e3",
                type="related_to",
            )
        )
        graph.add_relationship(
            Relationship(
                source_entity_id="e2",
                target_entity_id="e3",
                type="related_to",
            )
        )

        # e1 has high IDF (rare), e2 has low IDF (common)
        result = graph.personalized_pagerank({"e1": 5.0, "e2": 1.0})

        # Rare entity (e1) should get higher score due to IDF weighting
        assert result["e1"] > result["e2"]

    def test_scores_sum_approximately_one(self, graph: NetworkXGraph) -> None:
        """PPR is a probability distribution."""
        for i in range(10):
            graph.add_entity(Entity(id=f"e{i}", name=f"E{i}", type="concept"))
        for i in range(9):
            graph.add_relationship(
                Relationship(
                    source_entity_id=f"e{i}",
                    target_entity_id=f"e{i + 1}",
                    type="related_to",
                )
            )

        result = graph.personalized_pagerank({"e0": 1.0})
        assert sum(result.values()) == pytest.approx(1.0, abs=0.01)

    def test_zero_weight_seeds_filtered(self, graph: NetworkXGraph) -> None:
        """Seeds with weight 0 should be excluded."""
        graph.add_entity(Entity(id="e1", name="E1", type="concept"))
        result = graph.personalized_pagerank({"e1": 0.0})
        assert result == {}

    def test_negative_weight_seeds_filtered(self, graph: NetworkXGraph) -> None:
        """Seeds with negative weight should be excluded."""
        graph.add_entity(Entity(id="e1", name="E1", type="concept"))
        result = graph.personalized_pagerank({"e1": -1.0})
        assert result == {}
