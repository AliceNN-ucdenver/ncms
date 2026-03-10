"""NetworkX-based in-memory knowledge graph.

Stores entities as nodes and relationships as directed edges.
Supports neighbor queries and multi-hop traversal.
"""

from __future__ import annotations

import networkx as nx

from ncms.domain.models import Entity, Relationship


class NetworkXGraph:
    """In-memory knowledge graph using NetworkX DiGraph."""

    def __init__(self) -> None:
        self._graph = nx.DiGraph()
        # Track which entities are linked to which memories
        self._memory_entities: dict[str, set[str]] = {}  # memory_id -> set of entity_ids
        self._entity_memories: dict[str, set[str]] = {}  # entity_id -> set of memory_ids

    def add_entity(self, entity: Entity) -> None:
        self._graph.add_node(
            entity.id,
            name=entity.name,
            type=entity.type,
            attributes=entity.attributes,
        )

    def add_relationship(self, rel: Relationship) -> None:
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
        self._memory_entities.setdefault(memory_id, set()).add(entity_id)
        self._entity_memories.setdefault(entity_id, set()).add(memory_id)

    def get_neighbors(
        self,
        entity_id: str,
        relation_type: str | None = None,
        depth: int = 1,
    ) -> list[Entity]:
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

    def get_entity_ids_for_memory(self, memory_id: str) -> list[str]:
        return list(self._memory_entities.get(memory_id, set()))

    def get_related_memory_ids(self, entity_ids: list[str], depth: int = 1) -> set[str]:
        """Find memory IDs connected to the given entities (via graph traversal)."""
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
        return self._graph.number_of_nodes()

    def relationship_count(self) -> int:
        return self._graph.number_of_edges()

    def clear(self) -> None:
        self._graph.clear()
        self._memory_entities.clear()
        self._entity_memories.clear()
