"""Unit tests for entity co-occurrence clustering.

Tests that the clusterer correctly groups memories by shared entities,
respects min_cluster_size, excludes insight-type memories, and merges
overlapping groups via union-find.
"""

from __future__ import annotations

from ncms.domain.models import Entity, Memory
from ncms.infrastructure.consolidation.clusterer import find_entity_clusters
from ncms.infrastructure.graph.networkx_store import NetworkXGraph


def _make_memory(content: str, domains: list[str] | None = None, **kwargs) -> Memory:
    """Helper to build a Memory with minimal fields."""
    return Memory(content=content, domains=domains or [], **kwargs)


def _setup_graph_with_entities(
    graph: NetworkXGraph,
    memories: list[Memory],
    entity_map: dict[int, list[str]],
) -> dict[str, Entity]:
    """Create entities and link them to memories by index.

    entity_map: {memory_index: [entity_name, ...]}
    Returns a dict of entity_name -> Entity.
    """
    entities: dict[str, Entity] = {}
    for mem_idx, entity_names in entity_map.items():
        for name in entity_names:
            if name not in entities:
                ent = Entity(name=name, type="technology")
                entities[name] = ent
                graph.add_entity(ent)
            graph.link_memory_entity(memories[mem_idx].id, entities[name].id)
    return entities


class TestFindEntityClusters:
    """Tests for find_entity_clusters()."""

    def test_cluster_by_shared_entities(self):
        """Memories sharing entities should form a cluster."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("JWT token validation", domains=["auth"]),
            _make_memory("OAuth2 integration", domains=["auth"]),
            _make_memory("RBAC permission model", domains=["auth"]),
        ]
        # All three share "auth-service" entity; m0 and m1 share "token" entity
        _setup_graph_with_entities(graph, memories, {
            0: ["auth-service", "token"],
            1: ["auth-service", "token"],
            2: ["auth-service"],
        })

        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 1
        assert len(clusters[0].memories) == 3

    def test_min_cluster_size_filter(self):
        """Clusters below minimum size should be excluded."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("Memory A"),
            _make_memory("Memory B"),
        ]
        _setup_graph_with_entities(graph, memories, {
            0: ["shared-entity"],
            1: ["shared-entity"],
        })

        # min_cluster_size=3 but only 2 memories share entity
        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 0

    def test_min_cluster_size_two_works(self):
        """Clusters should form when min_cluster_size=2 and 2 memories share entity."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("Memory A"),
            _make_memory("Memory B"),
        ]
        _setup_graph_with_entities(graph, memories, {
            0: ["shared-entity"],
            1: ["shared-entity"],
        })

        clusters = find_entity_clusters(memories, graph, min_cluster_size=2)
        assert len(clusters) == 1
        assert len(clusters[0].memories) == 2

    def test_insights_excluded_from_clustering(self):
        """Memory(type='insight') should not appear in clusters."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("Regular memory A", domains=["db"]),
            _make_memory("Regular memory B", domains=["db"]),
            _make_memory("Regular memory C", domains=["db"]),
            Memory(content="An insight", type="insight", domains=["db"]),
        ]
        # All four share an entity, but the insight should be excluded
        _setup_graph_with_entities(graph, memories, {
            0: ["postgres"],
            1: ["postgres"],
            2: ["postgres"],
            3: ["postgres"],
        })

        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 1
        cluster_ids = {m.id for m in clusters[0].memories}
        assert memories[3].id not in cluster_ids, "Insight should not be in cluster"

    def test_no_entities_no_clusters(self):
        """Memories with no linked entities should not form clusters."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("Plain text memory A"),
            _make_memory("Plain text memory B"),
            _make_memory("Plain text memory C"),
        ]
        # No entity links

        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 0

    def test_cluster_tracks_shared_entities(self):
        """Cluster should track which entity IDs are shared (linked to 2+ memories)."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("Memory A"),
            _make_memory("Memory B"),
            _make_memory("Memory C"),
        ]
        entities = _setup_graph_with_entities(graph, memories, {
            0: ["shared-x", "unique-a"],
            1: ["shared-x", "shared-y"],
            2: ["shared-x", "shared-y"],
        })

        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 1
        # "shared-x" is linked to all three; "shared-y" to m1 and m2
        assert entities["shared-x"].id in clusters[0].shared_entity_ids
        assert entities["shared-y"].id in clusters[0].shared_entity_ids
        # "unique-a" is only linked to m0 — should NOT be shared
        assert entities["unique-a"].id not in clusters[0].shared_entity_ids

    def test_cluster_tracks_domains(self):
        """Cluster domains should be the union of all memory domains."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("Memory A", domains=["auth", "api"]),
            _make_memory("Memory B", domains=["db"]),
            _make_memory("Memory C", domains=["auth"]),
        ]
        _setup_graph_with_entities(graph, memories, {
            0: ["shared"],
            1: ["shared"],
            2: ["shared"],
        })

        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 1
        assert clusters[0].domains == {"auth", "api", "db"}

    def test_overlapping_clusters_merged(self):
        """Memories in multiple entity groups should merge into one cluster."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("Memory A"),
            _make_memory("Memory B"),
            _make_memory("Memory C"),
            _make_memory("Memory D"),
        ]
        # A-B share entity "x", B-C share entity "y", C-D share entity "z"
        # Union-find should merge all into one cluster
        _setup_graph_with_entities(graph, memories, {
            0: ["x"],
            1: ["x", "y"],
            2: ["y", "z"],
            3: ["z"],
        })

        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 1
        assert len(clusters[0].memories) == 4

    def test_two_separate_clusters(self):
        """Memories with disjoint entity sets should form separate clusters."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("Auth A"),
            _make_memory("Auth B"),
            _make_memory("Auth C"),
            _make_memory("DB X"),
            _make_memory("DB Y"),
            _make_memory("DB Z"),
        ]
        _setup_graph_with_entities(graph, memories, {
            0: ["auth-entity"],
            1: ["auth-entity"],
            2: ["auth-entity"],
            3: ["db-entity"],
            4: ["db-entity"],
            5: ["db-entity"],
        })

        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 2
        # Sorted by size (both are 3, so order may vary)
        cluster_sizes = sorted([len(c.memories) for c in clusters], reverse=True)
        assert cluster_sizes == [3, 3]

    def test_sorted_by_size_largest_first(self):
        """Clusters should be returned sorted by size, largest first."""
        graph = NetworkXGraph()
        memories = [
            _make_memory("A1"),
            _make_memory("A2"),
            _make_memory("A3"),
            _make_memory("A4"),
            _make_memory("B1"),
            _make_memory("B2"),
            _make_memory("B3"),
        ]
        _setup_graph_with_entities(graph, memories, {
            0: ["big-entity"],
            1: ["big-entity"],
            2: ["big-entity"],
            3: ["big-entity"],
            4: ["small-entity"],
            5: ["small-entity"],
            6: ["small-entity"],
        })

        clusters = find_entity_clusters(memories, graph, min_cluster_size=3)
        assert len(clusters) == 2
        assert len(clusters[0].memories) == 4  # big cluster first
        assert len(clusters[1].memories) == 3
