"""Memory Service - orchestrates storage, indexing, graph, and scoring.

This is the primary entry point for memory operations:
store, search, recall, and manage the full retrieval pipeline.
"""

from __future__ import annotations

import logging

from ncms.config import NCMSConfig
from ncms.domain.entity_extraction import extract_entity_names
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
    retrieval_probability,
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
        event_log: object | None = None,
    ):
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config or NCMSConfig()
        # Optional EventLog for dashboard observability (duck-typed to avoid import)
        self._event_log = event_log

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

        # Auto-extract entities from content + merge with manually provided ones
        auto_entities = extract_entity_names(content)
        manual = list(entities or [])
        manual_names = {e["name"].lower() for e in manual}
        all_entities = manual + [e for e in auto_entities if e["name"].lower() not in manual_names]

        for e_data in all_entities:
            entity = await self.add_entity(
                name=e_data["name"],
                entity_type=e_data.get("type", "concept"),
                attributes=e_data.get("attributes", {}),
            )
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
        if self._event_log:
            self._event_log.memory_stored(
                memory_id=memory.id,
                content_preview=content,
                memory_type=memory_type,
                domains=memory.domains,
                entity_count=len(all_entities),
                agent_id=source_agent,
            )
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

        # Extract entities from query for spreading activation context
        # Use graph O(1) name index when available, fall back to SQLite
        query_entity_names = extract_entity_names(query)
        context_entity_ids: list[str] = []
        for qe in query_entity_names:
            eid = self._graph.find_entity_by_name(qe["name"])
            if eid:
                context_entity_ids.append(eid)
            else:
                # Fall back to SQLite for entities not yet in graph
                existing = await self._store.find_entity_by_name(qe["name"])
                if existing:
                    context_entity_ids.append(existing.id)

        # Load full memory objects and compute activation scores
        scored: list[ScoredMemory] = []
        for memory_id, bm25_score in bm25_results:
            memory = await self._store.get_memory(memory_id)
            if not memory:
                continue

            # Domain filter (exact match or prefix match)
            if domain and domain not in memory.domains and not any(
                d.startswith(domain) for d in memory.domains
            ):
                continue

            # Tier 2: ACT-R activation scoring
            access_ages = await self._store.get_access_times(memory_id)
            bl = base_level_activation(access_ages, decay=self._config.actr_decay)

            # Spreading activation from graph via shared entities
            memory_entities = self._graph.get_entity_ids_for_memory(memory_id)
            spread = spreading_activation(
                memory_entity_ids=memory_entities,
                context_entity_ids=context_entity_ids,
                source_activation=self._config.actr_max_spread,
            )

            noise = activation_noise(sigma=self._config.actr_noise)
            act = total_activation(bl, spread, noise)

            # Combine BM25 and activation using configurable weights
            w_bm25 = self._config.scoring_weight_bm25
            w_actr = self._config.scoring_weight_actr
            combined = bm25_score * w_bm25 + act * w_actr

            # Compute retrieval probability for threshold filtering
            ret_prob = retrieval_probability(
                act,
                threshold=self._config.actr_threshold,
                tau=self._config.actr_temperature,
            )

            # Filter out very low probability candidates
            if ret_prob < 0.05:
                continue

            scored.append(
                ScoredMemory(
                    memory=memory,
                    bm25_score=bm25_score,
                    base_level=bl,
                    spreading=spread,
                    total_activation=combined,
                    retrieval_prob=ret_prob,
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

        # Sort by combined score (descending) — Tier 2 ranking
        scored.sort(key=lambda s: s.total_activation, reverse=True)

        # Tier 3: Optional LLM-as-judge reranking
        if self._config.llm_judge_enabled and scored:
            scored = await self._apply_llm_judge(query, scored)

        results = scored[:limit]
        if self._event_log:
            self._event_log.memory_searched(
                query=query,
                result_count=len(results),
                top_score=results[0].total_activation if results else None,
                agent_id=agent_id,
            )
        return results

    async def _apply_llm_judge(
        self, query: str, scored: list[ScoredMemory],
    ) -> list[ScoredMemory]:
        """Apply LLM-as-judge reranking to top candidates (Tier 3)."""
        from ncms.infrastructure.llm.judge import judge_relevance

        # Only judge the top-k candidates to control cost
        top_k = self._config.tier3_judge_top_k
        candidates = scored[:top_k]
        remainder = scored[top_k:]

        judge_results = await judge_relevance(
            query, candidates, model=self._config.llm_model,
        )

        # Build lookup of judge scores by memory_id
        judge_scores = {mid: score for mid, score in judge_results}

        # Blend: combined = activation * 0.4 + judge_relevance * 0.6
        reranked: list[ScoredMemory] = []
        for sm in candidates:
            judge_score = judge_scores.get(sm.memory.id, 0.5)
            blended = sm.total_activation * 0.4 + judge_score * 0.6
            reranked.append(
                ScoredMemory(
                    memory=sm.memory,
                    bm25_score=sm.bm25_score,
                    base_level=sm.base_level,
                    spreading=sm.spreading,
                    total_activation=blended,
                    retrieval_prob=sm.retrieval_prob,
                )
            )

        reranked.sort(key=lambda s: s.total_activation, reverse=True)
        return reranked + remainder

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

    async def add_entity(
        self, name: str, entity_type: str, attributes: dict | None = None,
    ) -> Entity:
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
