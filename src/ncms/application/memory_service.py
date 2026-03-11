"""Memory Service - orchestrates storage, indexing, graph, and scoring.

This is the primary entry point for memory operations:
store, search, recall, and manage the full retrieval pipeline.
"""

from __future__ import annotations

import logging
import time
import uuid

from ncms.config import NCMSConfig
from ncms.domain.entity_extraction import extract_entities
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
        splade: object | None = None,
    ):
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config or NCMSConfig()
        # Optional EventLog for dashboard observability (duck-typed to avoid import)
        self._event_log = event_log
        # Optional SPLADE engine for sparse neural retrieval (duck-typed)
        self._splade = splade

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
        pipeline_id = uuid.uuid4().hex[:12]
        pipeline_start = time.perf_counter()

        def _emit_stage(
            stage: str, duration_ms: float, data: dict | None = None,
            memory_id: str | None = None,
        ) -> None:
            if self._event_log:
                self._event_log.pipeline_stage(
                    pipeline_id=pipeline_id, pipeline_type="store", stage=stage,
                    duration_ms=duration_ms, data=data,
                    agent_id=source_agent, memory_id=memory_id,
                )

        _emit_stage("start", 0.0, {"content_preview": content[:120], "memory_type": memory_type})

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
        t0 = time.perf_counter()
        await self._store.save_memory(memory)
        _emit_stage("persist", (time.perf_counter() - t0) * 1000, memory_id=memory.id)

        # Index in Tantivy
        t0 = time.perf_counter()
        self._index.index_memory(memory)
        _emit_stage("bm25_index", (time.perf_counter() - t0) * 1000, memory_id=memory.id)

        # Index in SPLADE (if enabled)
        if self._splade is not None:
            t0 = time.perf_counter()
            try:
                self._splade.index_memory(memory)
            except Exception:
                logger.warning(
                    "SPLADE indexing failed for %s, continuing", memory.id, exc_info=True
                )
            _emit_stage("splade_index", (time.perf_counter() - t0) * 1000, memory_id=memory.id)

        # Auto-extract entities from content + merge with manually provided ones
        t0 = time.perf_counter()
        auto_entities = extract_entities(content, config=self._config)
        manual = list(entities or [])
        manual_names = {e["name"].lower() for e in manual}
        all_entities = manual + [e for e in auto_entities if e["name"].lower() not in manual_names]
        extractor = "gliner" if self._config.gliner_enabled else "regex"
        _emit_stage("entity_extraction", (time.perf_counter() - t0) * 1000, {
            "extractor": extractor,
            "auto_count": len(auto_entities),
            "manual_count": len(manual),
            "total_count": len(all_entities),
            "entity_names": [e["name"] for e in all_entities[:10]],
        }, memory_id=memory.id)

        t0 = time.perf_counter()
        for e_data in all_entities:
            entity = await self.add_entity(
                name=e_data["name"],
                entity_type=e_data.get("type", "concept"),
                attributes=e_data.get("attributes", {}),
            )
            await self._store.link_memory_entity(memory.id, entity.id)
            self._graph.link_memory_entity(memory.id, entity.id)
        _emit_stage("graph_linking", (time.perf_counter() - t0) * 1000, {
            "entities_linked": len(all_entities),
        }, memory_id=memory.id)

        # Extract keyword bridge nodes if enabled
        keyword_count = 0
        if self._config.keyword_bridge_enabled:
            t0 = time.perf_counter()
            try:
                from ncms.infrastructure.extraction.keyword_extractor import extract_keywords

                keywords = await extract_keywords(
                    content,
                    existing_entities=all_entities,
                    model=self._config.keyword_llm_model,
                    max_keywords=self._config.keyword_max_per_memory,
                    api_base=self._config.keyword_llm_api_base,
                )
                keyword_count = len(keywords)
                for kw in keywords:
                    kw_entity = await self.add_entity(
                        name=kw["name"],
                        entity_type="keyword",
                    )
                    await self._store.link_memory_entity(memory.id, kw_entity.id)
                    self._graph.link_memory_entity(memory.id, kw_entity.id)
            except Exception:
                logger.warning(
                    "Keyword extraction failed, continuing without keywords",
                    exc_info=True,
                )
            _emit_stage("keyword_bridge", (time.perf_counter() - t0) * 1000, {
                "keyword_count": keyword_count,
            }, memory_id=memory.id)

        # Contradiction detection (uses shared llm_model + llm_api_base)
        contradiction_count = 0
        candidates_checked = 0
        if self._config.contradiction_detection_enabled:
            t0 = time.perf_counter()
            try:
                from ncms.infrastructure.llm.contradiction_detector import (
                    detect_contradictions,
                )

                # Find similar existing memories (new memory already indexed)
                candidates = self._index.search(
                    content, limit=self._config.contradiction_candidate_limit + 1
                )
                candidate_ids = [mid for mid, _ in candidates if mid != memory.id]
                candidate_ids = candidate_ids[: self._config.contradiction_candidate_limit]

                # Also pull in graph-related memories via shared entities
                for e_data in all_entities[:5]:
                    eid = self._graph.find_entity_by_name(e_data["name"])
                    if eid:
                        related = self._graph.get_related_memory_ids([eid], depth=1)
                        for rid in related:
                            if rid != memory.id and rid not in candidate_ids:
                                candidate_ids.append(rid)
                                if len(candidate_ids) >= self._config.contradiction_candidate_limit:
                                    break

                # Domain-scope: only check overlapping domains
                candidate_memories: list[Memory] = []
                for cid in candidate_ids:
                    cmem = await self._store.get_memory(cid)
                    if cmem and (
                        not memory.domains
                        or not cmem.domains
                        or set(memory.domains) & set(cmem.domains)
                    ):
                        candidate_memories.append(cmem)

                candidates_checked = len(candidate_memories)
                if candidate_memories:
                    contradictions = await detect_contradictions(
                        new_memory=memory,
                        existing_memories=candidate_memories,
                        model=self._config.llm_model,
                        api_base=self._config.llm_api_base,
                    )

                    contradiction_count = len(contradictions)
                    if contradictions:
                        # Annotate the new memory
                        structured_data = dict(memory.structured or {})
                        structured_data["contradictions"] = contradictions
                        memory.structured = structured_data
                        await self._store.update_memory(memory)

                        # Annotate each contradicted existing memory
                        for c in contradictions:
                            existing = await self._store.get_memory(c["existing_memory_id"])
                            if existing:
                                ex_structured = dict(existing.structured or {})
                                ex_contradictions = ex_structured.get("contradicted_by", [])
                                ex_contradictions.append(
                                    {
                                        "newer_memory_id": memory.id,
                                        "contradiction_type": c["contradiction_type"],
                                        "explanation": c["explanation"],
                                        "severity": c["severity"],
                                    }
                                )
                                ex_structured["contradicted_by"] = ex_contradictions
                                existing.structured = ex_structured
                                await self._store.update_memory(existing)

                        logger.info(
                            "Detected %d contradiction(s) for memory %s",
                            len(contradictions),
                            memory.id,
                        )
            except Exception:
                logger.warning(
                    "Contradiction detection failed, continuing without contradictions",
                    exc_info=True,
                )
            _emit_stage("contradiction", (time.perf_counter() - t0) * 1000, {
                "candidates_checked": candidates_checked,
                "contradictions_found": contradiction_count,
            }, memory_id=memory.id)

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

        # Pipeline complete
        total_ms = (time.perf_counter() - pipeline_start) * 1000
        _emit_stage("complete", total_ms, {
            "memory_id": memory.id,
            "entity_count": len(all_entities),
            "total_duration_ms": round(total_ms, 2),
        }, memory_id=memory.id)

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
        pipeline_id = uuid.uuid4().hex[:12]
        pipeline_start = time.perf_counter()

        def _emit_stage(
            stage: str, duration_ms: float, data: dict | None = None,
        ) -> None:
            if self._event_log:
                self._event_log.pipeline_stage(
                    pipeline_id=pipeline_id, pipeline_type="search", stage=stage,
                    duration_ms=duration_ms, data=data, agent_id=agent_id,
                )

        _emit_stage("start", 0.0, {"query": query[:200], "domain": domain, "limit": limit})

        # Tier 1: BM25 candidate retrieval via Tantivy
        t0 = time.perf_counter()
        bm25_results = self._index.search(query, limit=self._config.tier1_candidates)
        bm25_data: dict[str, object] = {
            "candidate_count": len(bm25_results),
            "top_score": round(bm25_results[0][1], 3) if bm25_results else None,
        }
        if self._config.pipeline_debug and bm25_results:
            bm25_data["candidates"] = await self._load_candidate_previews(
                bm25_results[:20]
            )
        _emit_stage("bm25", (time.perf_counter() - t0) * 1000, bm25_data)

        # Tier 1 (parallel): SPLADE candidate retrieval (if enabled)
        splade_results: list[tuple[str, float]] = []
        if self._splade is not None:
            t0 = time.perf_counter()
            try:
                splade_results = self._splade.search(
                    query, limit=self._config.splade_top_k
                )
            except Exception:
                logger.warning("SPLADE search failed, using BM25 only", exc_info=True)
            splade_data: dict[str, object] = {
                "candidate_count": len(splade_results),
            }
            if self._config.pipeline_debug and splade_results:
                splade_data["candidates"] = (
                    await self._load_candidate_previews(
                        splade_results[:20]
                    )
                )
            _emit_stage(
                "splade", (time.perf_counter() - t0) * 1000, splade_data,
            )

        # Fuse BM25 + SPLADE via Reciprocal Rank Fusion
        if splade_results:
            t0 = time.perf_counter()
            fused_candidates = self._rrf_fuse(bm25_results, splade_results)
            rrf_data: dict[str, object] = {
                "fused_count": len(fused_candidates),
            }
            if self._config.pipeline_debug and fused_candidates:
                rrf_data["candidates"] = (
                    await self._load_candidate_previews(
                        fused_candidates[:20]
                    )
                )
            _emit_stage(
                "rrf_fusion", (time.perf_counter() - t0) * 1000, rrf_data,
            )
        else:
            fused_candidates = bm25_results

        if not fused_candidates:
            total_ms = (time.perf_counter() - pipeline_start) * 1000
            _emit_stage("complete", total_ms, {
                "result_count": 0, "total_candidates_evaluated": 0,
                "top_score": None, "total_duration_ms": round(total_ms, 2),
            })
            return []

        # Build per-source score lookups
        bm25_scores: dict[str, float] = {mid: score for mid, score in bm25_results}
        splade_scores: dict[str, float] = {mid: score for mid, score in splade_results}

        # Extract entities from query for spreading activation context
        # Use graph O(1) name index when available, fall back to SQLite
        t0 = time.perf_counter()
        query_entity_names = extract_entities(query, config=self._config)
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
        _emit_stage("entity_extraction", (time.perf_counter() - t0) * 1000, {
            "query_entities": [e["name"] for e in query_entity_names[:10]],
            "context_entity_count": len(context_entity_ids),
        })

        # ── Tier 1.5: Graph-expanded candidate discovery ────────────────
        # Collect entity IDs from fused hits, then discover related memories
        # via shared graph entities that search missed lexically.
        fused_ids = {mid for mid, _ in fused_candidates}
        all_candidates: list[tuple[str, float]] = list(fused_candidates)

        if self._config.graph_expansion_enabled:
            t0 = time.perf_counter()
            candidate_entity_pool: set[str] = set()
            for memory_id, _ in fused_candidates:
                entity_ids = self._graph.get_entity_ids_for_memory(memory_id)
                candidate_entity_pool.update(entity_ids)

            novel_count = 0
            if candidate_entity_pool:
                related_memory_ids = self._graph.get_related_memory_ids(
                    list(candidate_entity_pool),
                    depth=self._config.graph_expansion_depth,
                )
                novel_ids = related_memory_ids - fused_ids
                # Cap the expansion set
                if len(novel_ids) > self._config.graph_expansion_max:
                    novel_ids = set(list(novel_ids)[: self._config.graph_expansion_max])

                novel_count = len(novel_ids)
                for gid in novel_ids:
                    all_candidates.append((gid, 0.0))

                if novel_ids:
                    logger.debug(
                        "Graph expansion: %d novel candidates from %d entities",
                        len(novel_ids),
                        len(candidate_entity_pool),
                    )

            graph_exp_data: dict[str, object] = {
                "entity_pool_size": len(candidate_entity_pool),
                "novel_candidates": novel_count,
                "total_candidates": len(all_candidates),
            }
            if self._config.pipeline_debug and novel_count > 0:
                # novel IDs are the last novel_count entries
                novel_tuples = all_candidates[-novel_count:]
                graph_exp_data["candidates"] = (
                    await self._load_candidate_previews(
                        novel_tuples[:20]
                    )
                )
            _emit_stage(
                "graph_expansion",
                (time.perf_counter() - t0) * 1000,
                graph_exp_data,
            )

        # Load full memory objects and compute activation scores
        t0 = time.perf_counter()
        scored: list[ScoredMemory] = []
        candidates_scored = 0
        filtered_below_threshold = 0
        top_activation = 0.0
        for memory_id, _fused_score in all_candidates:
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

            # Look up per-source scores
            bm25_score = bm25_scores.get(memory_id, 0.0)
            splade_score_val = splade_scores.get(memory_id, 0.0)

            # Combine BM25, SPLADE, and activation using configurable weights
            w_bm25 = self._config.scoring_weight_bm25
            w_actr = self._config.scoring_weight_actr
            w_splade = self._config.scoring_weight_splade
            combined = bm25_score * w_bm25 + act * w_actr + splade_score_val * w_splade

            # Compute retrieval probability for threshold filtering
            ret_prob = retrieval_probability(
                act,
                threshold=self._config.actr_threshold,
                tau=self._config.actr_temperature,
            )

            candidates_scored += 1
            if combined > top_activation:
                top_activation = combined

            # Filter out very low probability candidates
            if ret_prob < 0.05:
                filtered_below_threshold += 1
                continue

            scored.append(
                ScoredMemory(
                    memory=memory,
                    bm25_score=bm25_score,
                    splade_score=splade_score_val,
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

        actr_data: dict[str, object] = {
            "candidates_scored": candidates_scored,
            "passed_threshold": len(scored),
            "filtered_below_threshold": filtered_below_threshold,
            "top_activation": round(top_activation, 3),
        }
        if self._config.pipeline_debug and scored:
            # Sort by activation before taking top 20
            debug_scored = sorted(
                scored, key=lambda s: s.total_activation, reverse=True,
            )
            actr_data["candidates"] = [
                {
                    "id": s.memory.id,
                    "content": s.memory.content[:120],
                    "score": round(s.total_activation, 3),
                    "bm25_score": round(s.bm25_score, 3),
                    "splade_score": round(s.splade_score, 3),
                    "base_level": round(s.base_level, 3),
                    "spreading": round(s.spreading, 3),
                    "total_activation": round(s.total_activation, 3),
                    "retrieval_prob": round(s.retrieval_prob, 3),
                }
                for s in debug_scored[:20]
            ]
        _emit_stage(
            "actr_scoring", (time.perf_counter() - t0) * 1000, actr_data,
        )

        # Sort by combined score (descending) — Tier 2 ranking
        scored.sort(key=lambda s: s.total_activation, reverse=True)

        # Tier 3: Optional LLM-as-judge reranking
        if self._config.llm_judge_enabled and scored:
            t0 = time.perf_counter()
            scored = await self._apply_llm_judge(query, scored)
            _emit_stage("llm_judge", (time.perf_counter() - t0) * 1000, {
                "judged_count": min(len(scored), self._config.tier3_judge_top_k),
                "model": self._config.llm_model,
            })

        results = scored[:limit]

        # Pipeline complete
        total_ms = (time.perf_counter() - pipeline_start) * 1000
        _emit_stage("complete", total_ms, {
            "result_count": len(results),
            "total_candidates_evaluated": candidates_scored,
            "top_score": round(results[0].total_activation, 3) if results else None,
            "total_duration_ms": round(total_ms, 2),
        })

        if self._event_log:
            self._event_log.memory_searched(
                query=query,
                result_count=len(results),
                top_score=results[0].total_activation if results else None,
                agent_id=agent_id,
            )
        return results

    async def _load_candidate_previews(
        self, candidates: list[tuple[str, float]], limit: int = 20,
    ) -> list[dict[str, object]]:
        """Load content previews for candidate IDs (debug mode only)."""
        result: list[dict[str, object]] = []
        for mid, score in candidates[:limit]:
            memory = await self._store.get_memory(mid)
            result.append({
                "id": mid,
                "score": round(score, 3),
                "content": (
                    memory.content[:120] if memory else "(not found)"
                ),
            })
        return result

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
            api_base=self._config.llm_api_base,
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
        if self._splade is not None:
            self._splade.remove(memory_id)
        await self._store.delete_memory(memory_id)

    @staticmethod
    def _rrf_fuse(
        bm25_results: list[tuple[str, float]],
        splade_results: list[tuple[str, float]],
        k: int = 60,
    ) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion of two result lists.

        RRF score = sum(1 / (k + rank_i)) across all lists where the doc appears.
        k=60 is the standard constant from the original RRF paper (Cormack et al. 2009).

        Returns fused (memory_id, rrf_score) list sorted descending.
        """
        rrf_scores: dict[str, float] = {}

        for rank, (mid, _score) in enumerate(bm25_results):
            rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

        for rank, (mid, _score) in enumerate(splade_results):
            rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

        fused = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        return fused

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
