"""Graph Service - higher-level knowledge graph operations."""

from __future__ import annotations

from ncms.domain.models import Entity
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


class GraphService:
    """Higher-level operations on the knowledge graph."""

    def __init__(self, store: SQLiteStore, graph: NetworkXGraph):
        self._store = store
        self._graph = graph

    async def rebuild_from_store(self) -> None:
        """Rebuild the in-memory graph from SQLite (for rehydration)."""
        self._graph.clear()

        entities = await self._store.list_entities()
        for entity in entities:
            self._graph.add_entity(entity)

        # Rebuild relationships for each entity
        seen_rels: set[str] = set()
        for entity in entities:
            rels = await self._store.get_relationships(entity.id)
            for rel in rels:
                if rel.id not in seen_rels:
                    self._graph.add_relationship(rel)
                    seen_rels.add(rel.id)

        # Rebuild memory-entity links
        memories = await self._store.list_memories(limit=100000)
        for memory in memories:
            entity_ids = await self._store.get_memory_entities(memory.id)
            for eid in entity_ids:
                self._graph.link_memory_entity(memory.id, eid)

    def get_neighbors(
        self, entity_id: str, relation_type: str | None = None, depth: int = 1
    ) -> list[Entity]:
        return self._graph.get_neighbors(entity_id, relation_type, depth)

    def get_related_memory_ids(self, entity_ids: list[str], depth: int = 1) -> set[str]:
        return self._graph.get_related_memory_ids(entity_ids, depth)
