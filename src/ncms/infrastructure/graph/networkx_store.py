"""NetworkX-based in-memory knowledge graph.

Stores entities as nodes and relationships as directed edges.
Supports neighbor queries and multi-hop traversal.

Thread-safe: all mutations and reads are protected by an RLock
to support concurrent store_memory calls via asyncio.to_thread.
"""

from __future__ import annotations

import threading

import networkx as nx

from ncms.domain.models import Entity, Relationship


class NetworkXGraph:
    """In-memory knowledge graph using NetworkX DiGraph."""

    def __init__(self) -> None:
        self._graph = nx.DiGraph()
        # Track which entities are linked to which memories
        self._memory_entities: dict[str, set[str]] = {}  # memory_id -> set of entity_ids
        self._entity_memories: dict[str, set[str]] = {}  # entity_id -> set of memory_ids
        # O(1) name → entity_id lookup (lowercase keys)
        self._name_index: dict[str, str] = {}
        self._lock = threading.RLock()

    def add_entity(self, entity: Entity) -> None:
        with self._lock:
            self._graph.add_node(
                entity.id,
                name=entity.name,
                type=entity.type,
                attributes=entity.attributes,
            )
            self._name_index[entity.name.lower()] = entity.id

    def add_relationship(self, rel: Relationship) -> None:
        with self._lock:
            self._graph.add_edge(
                rel.source_entity_id,
                rel.target_entity_id,
                id=rel.id,
                type=rel.type,
                valid_at=rel.valid_at,
                invalid_at=rel.invalid_at,
                source_memory_id=rel.source_memory_id,
            )

    def link_memory_entity(self, memory_id: str, entity_id: str) -> None:
        with self._lock:
            self._memory_entities.setdefault(memory_id, set()).add(entity_id)
            self._entity_memories.setdefault(entity_id, set()).add(memory_id)

    def get_neighbors(
        self,
        entity_id: str,
        relation_type: str | None = None,
        depth: int = 1,
    ) -> list[Entity]:
        with self._lock:
            if entity_id not in self._graph:
                return []

            visited: set[str] = set()
            current_layer = {entity_id}

            for _ in range(depth):
                next_layer: set[str] = set()
                for node in current_layer:
                    # Outgoing edges
                    for _, target, data in self._graph.out_edges(node, data=True):
                        if relation_type and data.get("type") != relation_type:
                            continue
                        if target not in visited and target != entity_id:
                            next_layer.add(target)
                    # Incoming edges
                    for source, _, data in self._graph.in_edges(node, data=True):
                        if relation_type and data.get("type") != relation_type:
                            continue
                        if source not in visited and source != entity_id:
                            next_layer.add(source)
                visited |= next_layer
                current_layer = next_layer

            entities = []
            for nid in visited:
                node_data = self._graph.nodes[nid]
                entities.append(
                    Entity(
                        id=nid,
                        name=node_data.get("name", ""),
                        type=node_data.get("type", "unknown"),
                        attributes=node_data.get("attributes", {}),
                    )
                )
            return entities

    def find_entity_by_name(self, name: str) -> str | None:
        """O(1) lookup of entity ID by name (case-insensitive)."""
        with self._lock:
            return self._name_index.get(name.lower())

    def get_entity_name(self, entity_id: str) -> str | None:
        """Return entity name by ID, or None if not in graph."""
        with self._lock:
            data = self._graph.nodes.get(entity_id)
            return data.get("name") if data else None

    def get_entity_ids_for_memory(self, memory_id: str) -> list[str]:
        with self._lock:
            return list(self._memory_entities.get(memory_id, set()))

    def get_related_memory_ids(self, entity_ids: list[str], depth: int = 1) -> set[str]:
        """Find memory IDs connected to the given entities (via graph traversal)."""
        with self._lock:
            related_entities: set[str] = set(entity_ids)

            # Expand through graph edges
            current = set(entity_ids)
            for _ in range(depth):
                next_set: set[str] = set()
                for eid in current:
                    if eid in self._graph:
                        for _, target, _ in self._graph.out_edges(eid, data=True):
                            next_set.add(target)
                        for source, _, _ in self._graph.in_edges(eid, data=True):
                            next_set.add(source)
                related_entities |= next_set
                current = next_set

            # Collect all memories linked to these entities
            memory_ids: set[str] = set()
            for eid in related_entities:
                memory_ids |= self._entity_memories.get(eid, set())
            return memory_ids

    def entity_count(self) -> int:
        with self._lock:
            return self._graph.number_of_nodes()

    def relationship_count(self) -> int:
        with self._lock:
            return self._graph.number_of_edges()

    def clear(self) -> None:
        with self._lock:
            self._graph.clear()
            self._memory_entities.clear()
            self._entity_memories.clear()
            self._name_index.clear()

    # ── Phase 8: Centrality + Entity-Memory Lookup ───────────────────

    def pagerank(self, alpha: float = 0.85) -> dict[str, float]:
        """Compute PageRank centrality over the entity graph.

        Pure-Python power iteration (no scipy dependency).
        Returns a dict mapping entity_id → centrality score (sums to ~1.0).
        Returns empty dict if the graph has no nodes.
        """
        with self._lock:
            if self._graph.number_of_nodes() == 0:
                return {}

            nodes = list(self._graph.nodes())
            n = len(nodes)
            if n == 0:
                return {}

            # Initialize uniform
            rank: dict[str, float] = {node: 1.0 / n for node in nodes}

            # Power iteration (50 iterations is plenty for convergence)
            for _ in range(50):
                new_rank: dict[str, float] = {}
                # Dangling node mass (nodes with no outgoing edges)
                dangling_sum = sum(
                    rank[node] for node in nodes
                    if self._graph.out_degree(node) == 0
                )

                for node in nodes:
                    # Teleport + dangling redistribution
                    new_rank[node] = (1.0 - alpha) / n + alpha * dangling_sum / n

                    # Incoming link contributions
                    for pred in self._graph.predecessors(node):
                        out_deg = self._graph.out_degree(pred)
                        if out_deg > 0:
                            new_rank[node] += alpha * rank[pred] / out_deg

                rank = new_rank

            return rank

    def get_memory_ids_for_entity(self, entity_id: str) -> set[str]:
        """Return all memory IDs linked to a specific entity."""
        with self._lock:
            return self._entity_memories.get(entity_id, set()).copy()

    def get_entity_document_frequency(self) -> dict[str, int]:
        """Return entity_id → number of memories it appears in (for IDF)."""
        with self._lock:
            return {eid: len(mids) for eid, mids in self._entity_memories.items()}

    def total_memory_count(self) -> int:
        """Return total number of memories with entity links."""
        with self._lock:
            return len(self._memory_entities)

    def get_edge_weight(self, source_id: str, target_id: str) -> float:
        """Return the edge weight between two entities (0.0 if no edge)."""
        with self._lock:
            if self._graph.has_edge(source_id, target_id):
                return self._graph[source_id][target_id].get("weight", 1.0)
            return 0.0

    def set_edge_weight(self, source_id: str, target_id: str, weight: float) -> None:
        """Set the weight on an existing edge."""
        with self._lock:
            if self._graph.has_edge(source_id, target_id):
                self._graph[source_id][target_id]["weight"] = weight

    def get_cooccurrence_edges(self) -> list[tuple[str, str, int]]:
        """Return all co-occurrence edges as (source, target, cooc_count) triples."""
        result: list[tuple[str, str, int]] = []
        with self._lock:
            for source, target, data in self._graph.edges(data=True):
                if data.get("type") == "co_occurs":
                    result.append((source, target, data.get("cooc_count", 1)))
        return result

    def get_neighbors_with_weights(
        self, entity_id: str,
    ) -> list[tuple[str, float]]:
        """Return (neighbor_id, edge_weight) for all neighbors of an entity."""
        with self._lock:
            if entity_id not in self._graph:
                return []
            neighbors: list[tuple[str, float]] = []
            seen: set[str] = set()
            # Outgoing
            for _, target, data in self._graph.out_edges(entity_id, data=True):
                if target not in seen:
                    neighbors.append((target, data.get("weight", 1.0)))
                    seen.add(target)
            # Incoming
            for source, _, data in self._graph.in_edges(entity_id, data=True):
                if source not in seen:
                    neighbors.append((source, data.get("weight", 1.0)))
                    seen.add(source)
            return neighbors

    def increment_edge_cooccurrence(
        self, source_id: str, target_id: str,
    ) -> int:
        """Increment co-occurrence count on an edge. Returns new count.

        If edge exists, increments its 'cooc_count' attribute.
        Returns 0 if edge doesn't exist.
        """
        with self._lock:
            if self._graph.has_edge(source_id, target_id):
                count = self._graph[source_id][target_id].get("cooc_count", 1) + 1
                self._graph[source_id][target_id]["cooc_count"] = count
                return count
            return 0

    def get_edge_cooccurrence(self, source_id: str, target_id: str) -> int:
        """Return co-occurrence count for an edge (0 if no edge)."""
        with self._lock:
            if self._graph.has_edge(source_id, target_id):
                return self._graph[source_id][target_id].get("cooc_count", 1)
            return 0

    def get_entity_degree(self, entity_id: str) -> int:
        """Return total degree (in + out) for an entity node.

        Used for hub-node dampening in graph spreading activation.
        High-degree nodes (e.g., generic entities like 'django', 'model')
        get dampened to prevent flooding activation across the graph.
        """
        with self._lock:
            if entity_id not in self._graph:
                return 0
            return (
                self._graph.in_degree(entity_id)
                + self._graph.out_degree(entity_id)
            )

    # ── Phase 9: Personalized PageRank ────────────────────────────────

    def personalized_pagerank(
        self,
        seed_entities: dict[str, float],
        alpha: float = 0.85,
        max_iter: int = 50,
        tol: float = 1e-6,
    ) -> dict[str, float]:
        """Compute Personalized PageRank from seed entities.

        Query-conditioned graph scoring: activation flows from seed (query)
        entities through the graph. Entities close to the query via many
        high-weight paths get higher scores (HippoRAG-style).

        Args:
            seed_entities: entity_id → IDF weight (will be normalized to sum 1.0).
            alpha: Damping factor (probability of following links vs teleporting).
            max_iter: Maximum power iteration steps.
            tol: Convergence tolerance.

        Returns:
            Dict mapping entity_id → PPR score (probability, sums to ~1.0).
            Empty dict if graph has no nodes or no valid seeds.
        """
        with self._lock:
            if self._graph.number_of_nodes() == 0 or not seed_entities:
                return {}

            # Filter seeds to only entities present in the graph
            valid_seeds = {
                eid: w for eid, w in seed_entities.items()
                if eid in self._graph and w > 0
            }
            if not valid_seeds:
                return {}

            # Normalize to probability distribution (sum to 1.0)
            total_w = sum(valid_seeds.values())
            if total_w <= 0:
                return {}
            personalization = {eid: w / total_w for eid, w in valid_seeds.items()}

            try:
                return nx.pagerank(
                    self._graph,
                    alpha=alpha,
                    personalization=personalization,
                    max_iter=max_iter,
                    tol=tol,
                )
            except nx.PowerIterationFailedConvergence:
                # Return last iteration result via lower tolerance
                try:
                    return nx.pagerank(
                        self._graph,
                        alpha=alpha,
                        personalization=personalization,
                        max_iter=max_iter * 2,
                        tol=tol * 100,
                    )
                except Exception:
                    return {}
            except Exception:
                return {}
