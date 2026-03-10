"""Memory Service - orchestrates storage, indexing, graph, and scoring.

This is the primary entry point for memory operations:
store, search, recall, and manage the full retrieval pipeline.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ncms.config import NCMSConfig
from ncms.domain.models import (
    AccessRecord,
    Entity,
    Memory,
    Relationship,
    ScoredMemory,
)
from ncms.domain.scoring import (
    activation_noise,
    base_level_activation,
    spreading_activation,
    total_activation,
)
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class MemoryService:
    """Orchestrates the full memory lifecycle: store, index, search, score."""

    def __init__(
        self,
        store: SQLiteStore,
        index: TantivyEngine,
        graph: NetworkXGraph,
        config: NCMSConfig | None = None,
    ):
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config or NCMSConfig()

    @property
    def store(self) -> SQLiteStore:
        return self._store

    @property
    def graph(self) -> NetworkXGraph:
        return self._graph

    # ── Store ────────────────────────────────────────────────────────────

    async def store_memory(
        self,
        content: str,
        memory_type: str = "fact",
        domains: list[str] | None = None,
        tags: list[str] | None = None,
        source_agent: str | None = None,
        project: str | None = None,
        structured: dict | None = None,
        importance: float = 5.0,
        entities: list[dict] | None = None,
        relationships: list[dict] | None = None,
    ) -> Memory:
        """Store a new memory with automatic indexing and graph updates."""
        memory = Memory(
            content=content,
            type=memory_type,
            domains=domains or [],
            tags=tags or [],
            source_agent=source_agent,
            project=project,
            structured=structured,
            importance=importance,
        )

        # Persist to SQLite
        await self._store.save_memory(memory)

        # Index in Tantivy
        self._index.index_memory(memory)

        # Process entities if provided
        if entities:
            for e_data in entities:
                entity = Entity(
                    name=e_data["name"],
                    type=e_data.get("type", "concept"),
                    attributes=e_data.get("attributes", {}),
                )
                await self._store.save_entity(entity)
                self._graph.add_entity(entity)
                await self._store.link_memory_entity(memory.id, entity.id)
                self._graph.link_memory_entity(memory.id, entity.id)

        # Process relationships if provided
        if relationships:
            for r_data in relationships:
                rel = Relationship(
                    source_entity_id=r_data["source"],
                    target_entity_id=r_data["target"],
                    type=r_data.get("type", "related_to"),
                    source_memory_id=memory.id,
                )
                await self._store.save_relationship(rel)
                self._graph.add_relationship(rel)

        # Log initial access
        await self._store.log_access(
            AccessRecord(memory_id=memory.id, accessing_agent=source_agent)
        )

        logger.info("Stored memory %s: %s", memory.id, content[:80])
        return memory

    # ── Search ───────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        domain: str | None = None,
        limit: int = 10,
        agent_id: str | None = None,
    ) -> list[ScoredMemory]:
        """Execute the full retrieval pipeline: BM25 -> ACT-R rescoring."""

        # Tier 1: BM25 candidate retrieval via Tantivy
        bm25_results = self._index.search(query, limit=self._config.tier1_candidates)

        if not bm25_results:
            return []

        # Load full memory objects and compute activation scores
        scored: list[ScoredMemory] = []
        for memory_id, bm25_score in bm25_results:
            memory = await self._store.get_memory(memory_id)
            if not memory:
                continue

            # Domain filter
            if domain and domain not in memory.domains:
                # Check prefix match
                if not any(d.startswith(domain) for d in memory.domains):
                    continue

            # Tier 2: ACT-R activation scoring
            access_ages = await self._store.get_access_times(memory_id)
            bl = base_level_activation(access_ages, decay=self._config.actr_decay)

            # Spreading activation from graph
            memory_entities = self._graph.get_entity_ids_for_memory(memory_id)
            # For now, context entities are empty (would come from query entity extraction)
            spread = spreading_activation(
                memory_entity_ids=memory_entities,
                context_entity_ids=[],
                source_activation=self._config.actr_max_spread,
            )

            noise = activation_noise(sigma=self._config.actr_noise)
            act = total_activation(bl, spread, noise)

            # Combine BM25 and activation
            combined = bm25_score * 0.6 + act * 0.4

            scored.append(
                ScoredMemory(
                    memory=memory,
                    bm25_score=bm25_score,
                    base_level=bl,
                    spreading=spread,
                    total_activation=combined,
                )
            )

            # Log access for future ACT-R scoring
            await self._store.log_access(
                AccessRecord(
                    memory_id=memory_id,
                    accessing_agent=agent_id,
                    query_context=query,
                )
            )

        # Sort by combined score (descending)
        scored.sort(key=lambda s: s.total_activation, reverse=True)
        return scored[:limit]

    # ── Direct Access ────────────────────────────────────────────────────

    async def get_memory(self, memory_id: str) -> Memory | None:
        return await self._store.get_memory(memory_id)

    async def list_memories(
        self,
        domain: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[Memory]:
        return await self._store.list_memories(domain=domain, agent_id=agent_id, limit=limit)

    async def delete_memory(self, memory_id: str) -> None:
        self._index.remove(memory_id)
        await self._store.delete_memory(memory_id)

    # ── Entity Operations ────────────────────────────────────────────────

    async def add_entity(self, name: str, entity_type: str, attributes: dict | None = None) -> Entity:
        # Check for existing entity with same name
        existing = await self._store.find_entity_by_name(name)
        if existing:
            return existing

        entity = Entity(name=name, type=entity_type, attributes=attributes or {})
        await self._store.save_entity(entity)
        self._graph.add_entity(entity)
        return entity

    async def add_relationship(
        self,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        memory_id: str | None = None,
    ) -> Relationship:
        rel = Relationship(
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            type=relation_type,
            source_memory_id=memory_id,
        )
        await self._store.save_relationship(rel)
        self._graph.add_relationship(rel)
        return rel

    async def list_entities(self, entity_type: str | None = None) -> list[Entity]:
        return await self._store.list_entities(entity_type)

    # ── Stats ────────────────────────────────────────────────────────────

    async def memory_count(self) -> int:
        memories = await self._store.list_memories(limit=100000)
        return len(memories)

    def entity_count(self) -> int:
        return self._graph.entity_count()

    def relationship_count(self) -> int:
        return self._graph.relationship_count()
