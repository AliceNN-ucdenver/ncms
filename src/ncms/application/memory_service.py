"""Memory Service - orchestrates storage, indexing, graph, and scoring.

This is the primary entry point for memory operations:
store, search, recall, and manage the full retrieval pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from ncms.application.enrichment import EnrichmentPipeline
from ncms.application.ingestion import IngestionPipeline
from ncms.application.retrieval import RetrievalPipeline
from ncms.application.scoring import ScoringPipeline
from ncms.application.traversal import TraversalPipeline
from ncms.config import NCMSConfig
from ncms.domain.entity_extraction import resolve_labels
from ncms.domain.intent import IntentResult, QueryIntent, classify_intent
from ncms.domain.models import (
    AccessRecord,
    Entity,
    Memory,
    RecallResult,
    Relationship,
    ScoredMemory,
    SearchLogEntry,
    SynthesisMode,
    SynthesizedResponse,
    TopicCluster,
    TraversalMode,
    TraversalResult,
)
from ncms.domain.protocols import GraphEngine, IndexEngine, IntentClassifier, MemoryStore
from ncms.domain.temporal_parser import (
    TemporalReference,
    parse_temporal_reference,
)
from ncms.infrastructure.observability.event_log import (
    DashboardEvent,
    EventLog,
    NullEventLog,
)

if TYPE_CHECKING:
    from ncms.application.admission_service import AdmissionService
    from ncms.application.document_service import DocumentService
    from ncms.application.episode_service import EpisodeService
    from ncms.application.index_worker import IndexWorkerPool
    from ncms.application.reconciliation_service import ReconciliationService
    from ncms.application.section_service import SectionService
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.reranking.cross_encoder_reranker import CrossEncoderReranker

logger = logging.getLogger(__name__)


class MemoryService:
    """Orchestrates the full memory lifecycle: store, index, search, score."""

    def __init__(
        self,
        store: MemoryStore,
        index: IndexEngine,
        graph: GraphEngine,
        config: NCMSConfig | None = None,
        event_log: EventLog | NullEventLog | None = None,
        splade: SpladeEngine | None = None,
        admission: AdmissionService | None = None,
        reconciliation: ReconciliationService | None = None,
        episode: EpisodeService | None = None,
        intent_classifier: IntentClassifier | None = None,
        reranker: CrossEncoderReranker | None = None,
        section_service: SectionService | None = None,
        document_service: DocumentService | None = None,
    ):
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config or NCMSConfig()
        # EventLog for dashboard observability (NullEventLog discards events silently)
        self._event_log: EventLog | NullEventLog = event_log or NullEventLog()
        # Optional SPLADE engine for sparse neural retrieval
        self._splade = splade
        # Optional AdmissionService for Phase 1 admission scoring
        self._admission = admission
        # Optional ReconciliationService for Phase 2 state reconciliation
        self._reconciliation = reconciliation
        # Optional EpisodeService for Phase 3 episode formation
        self._episode = episode
        # Optional BM25 exemplar intent classifier (Phase 4)
        self._intent_classifier = intent_classifier
        # Optional cross-encoder reranker (Phase 10)
        self._reranker = reranker
        # Optional SectionService for content-aware ingestion
        self._section_svc = section_service
        # Optional DocumentService for document profile expansion
        self._document_service = document_service
        # Background indexing worker pool (Phase 2 performance)
        self._index_pool: IndexWorkerPool | None = None

        # Scoring pipeline (multi-signal ranking)
        self._scoring = ScoringPipeline(
            store=self._store,
            graph=self._graph,
            event_log=self._event_log,
            config=self._config,
        )

        # Retrieval pipeline (candidate discovery, rerank, expand)
        self._retrieval = RetrievalPipeline(
            store=self._store,
            index=self._index,
            graph=self._graph,
            config=self._config,
            splade=self._splade,
            reranker=self._reranker,
            get_cached_labels=self._get_cached_labels,
        )

        # Enrichment pipeline (recall bonuses + context decoration)
        self._enrichment = EnrichmentPipeline(
            store=self._store,
            graph=self._graph,
            document_service=self._document_service,
        )

        # Traversal pipeline (HTMG walks + topic clustering)
        self._traversal = TraversalPipeline(
            store=self._store,
            graph=self._graph,
            config=self._config,
        )

        # Ingestion pipeline (gates, indexing, node creation)
        self._ingestion = IngestionPipeline(
            store=self._store,
            index=self._index,
            graph=self._graph,
            event_log=self._event_log,
            config=self._config,
            splade=self._splade,
            admission=self._admission,
            reconciliation=self._reconciliation,
            episode=self._episode,
            section_service=self._section_svc,
            get_cached_labels=self._get_cached_labels,
            add_entity=self.add_entity,
        )

        # Log active feature flags for diagnostics
        features = []
        if self._config.splade_enabled:
            features.append("SPLADE")
        if self._config.admission_enabled:
            features.append("admission")
        if self._config.reconciliation_enabled:
            features.append("reconciliation")
        if self._config.episodes_enabled:
            features.append("episodes")
        if self._config.intent_classification_enabled:
            features.append("intent")
        if self._config.content_classification_enabled:
            features.append("content_classification")
        if self._config.temporal_enabled:
            features.append("temporal")
        if self._config.dream_cycle_enabled:
            features.append("dream")
        if self._config.reranker_enabled:
            features.append("reranker")
        features.append("async_indexing")  # Always on
        if self._config.level_first_enabled:
            features.append("level_first")
        if self._config.synthesis_enabled:
            features.append("synthesis")
        if self._config.topic_map_enabled:
            features.append("topic_map")
        logger.info("[memory_service] Active features: %s", ", ".join(features) or "none")

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def graph(self) -> GraphEngine:
        return self._graph

    async def start_index_pool(self, queue_size: int | None = None) -> None:
        """Start background indexing workers.

        Args:
            queue_size: Override queue capacity (e.g., for bulk import).
        """
        from ncms.application.index_worker import IndexWorkerPool
        pool = IndexWorkerPool(
            memory_service=self,
            num_workers=self._config.index_workers,
            queue_size=queue_size or self._config.index_queue_size,
            max_retries=self._config.index_max_retries,
            drain_timeout_seconds=self._config.index_drain_timeout_seconds,
        )
        await pool.start()
        self._index_pool = pool
        logger.info("Background indexing pool started: %d workers", self._config.index_workers)

    async def stop_index_pool(self) -> None:
        """Drain queue and stop background indexing workers."""
        if self._index_pool is not None:
            await self._index_pool.shutdown()  # type: ignore[union-attr]
            self._index_pool = None
            logger.info("Background indexing pool stopped")

    async def flush_indexing(self, poll_interval: float = 0.2) -> None:
        """Wait for the background index queue to drain completely.

        Use after bulk imports to ensure all memories are indexed before
        querying.  Returns immediately if no pool is running.
        """
        if self._index_pool is None:
            return
        while True:
            stats = self._index_pool.stats()  # type: ignore[union-attr]
            if stats.queue_depth == 0 and stats.workers_busy == 0:
                break
            await asyncio.sleep(poll_interval)
        logger.info("Index queue flushed — all memories indexed")

    def index_pool_stats(self) -> dict | None:
        """Return indexing pool stats, or None if not running."""
        if self._index_pool is None:
            return None
        from dataclasses import asdict
        return asdict(self._index_pool.stats())  # type: ignore[union-attr]

    async def _get_cached_labels(self, domains: list[str]) -> dict:
        """Load domain-specific entity labels from consolidation_state."""
        import json as _json

        cached: dict = {}
        for domain in domains:
            raw = await self._store.get_consolidation_value(f"entity_labels:{domain}")
            if raw:
                try:
                    labels = _json.loads(raw)
                    if isinstance(labels, list):
                        cached[domain] = labels
                except Exception:
                    pass
        # Load keep_universal preference
        raw_ku = await self._store.get_consolidation_value("_keep_universal")
        if raw_ku:
            with contextlib.suppress(Exception):
                cached["_keep_universal"] = _json.loads(raw_ku)
        return cached

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
            self._event_log.pipeline_stage(
                pipeline_id=pipeline_id, pipeline_type="store", stage=stage,
                duration_ms=duration_ms, data=data,
                agent_id=source_agent, memory_id=memory_id,
            )

        _emit_stage("start", 0.0, {"content_preview": content[:120], "memory_type": memory_type})

        # ── Pre-admission gates: dedup, size check, classification ────────
        gate_result = await self._ingestion.pre_admission_gates(
            content=content, memory_type=memory_type,
            importance=importance, tags=tags, structured=structured,
            source_agent=source_agent, emit_stage=_emit_stage,
            pipeline_start=pipeline_start,
        )
        if isinstance(gate_result, Memory):
            return gate_result  # dedup hit or navigable classification
        content_hash, tags = gate_result

        # ── Admission scoring (Phase 1, optional) ────────────────────────
        admission_route: str | None = None
        admission_features: object | None = None
        if self._admission is not None and self._config.admission_enabled:
            result = await self._ingestion.gate_admission(
                content=content, domains=domains, tags=tags,
                source_agent=source_agent, project=project,
                memory_type=memory_type, importance=importance,
                structured=structured,
                emit_stage=_emit_stage, pipeline_start=pipeline_start,
            )
            if isinstance(result, Memory):
                return result  # discard or ephemeral — early exit
            admission_route, admission_features, structured = result

        memory = Memory(
            content=content,
            type=cast(Any, memory_type),
            domains=domains or [],
            tags=tags or [],
            source_agent=source_agent,
            project=project,
            structured=structured,
            importance=importance,
            content_hash=content_hash,
        )

        # Persist to SQLite
        t0 = time.perf_counter()
        await self._store.save_memory(memory)
        _emit_stage("persist", (time.perf_counter() - t0) * 1000, memory_id=memory.id)

        # ── Background indexing (fast path) ─────────────────────────────
        # If the async index pool is running, enqueue ALL indexing work
        # (BM25, SPLADE, GLiNER, entities, episodes) and return immediately.
        # Admission scoring is pure text heuristics — no index dependency.
        # Content-hash dedup (above) handles exact duplicates via SQLite.
        if self._index_pool is not None:
            from ncms.application.index_worker import IndexTask

            task = IndexTask(
                memory_id=memory.id,
                content=content,
                memory_type=memory_type,
                domains=domains or [],
                tags=tags or [],
                source_agent=source_agent,
                importance=importance,
                entities_manual=list(entities or []),
                relationships=list(relationships or []),
                admission_features=admission_features,
                admission_route=admission_route,
            )
            enqueued = self._index_pool.enqueue(task)  # type: ignore[union-attr]
            if enqueued:
                _emit_stage("enqueued", (time.perf_counter() - pipeline_start) * 1000, {
                    "task_id": task.task_id,
                    "queue_depth": self._index_pool.stats().queue_depth,  # type: ignore[union-attr]
                }, memory_id=memory.id)
                memory.structured = {**(memory.structured or {}), "indexing": "queued"}
                logger.info(
                    "Stored+enqueued memory %s: %s", memory.id, content[:80],
                )
                return memory
            # Queue full — fall through to inline indexing (BM25 already done)
            logger.warning(
                "Index queue full, falling back to inline for %s", memory.id,
            )

        # ── Inline indexing (fallback / async_indexing disabled) ─────────
        all_entities, linked_entity_ids = (
            await self._ingestion.run_inline_indexing(
                memory=memory, content=content, domains=domains,
                entities_manual=entities, emit_stage=_emit_stage,
            )
        )

        # Contradiction detection — fire-and-forget async task (deferred).
        # Memory is already stored and indexed; contradiction is metadata
        # enrichment, not a gate.  This avoids blocking ingestion for the
        # 500-2000ms LLM round-trip.
        if self._config.contradiction_detection_enabled:
            asyncio.create_task(
                self._ingestion.deferred_contradiction_check(
                    memory=memory,
                    all_entities=all_entities,
                    pipeline_id=pipeline_id,
                    source_agent=source_agent,
                )
            )

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

        # Write MemoryNodes (additive layering: L1 atomic always, L2 entity_state if detected)
        _should_create_node = (
            admission_route == "persist"
            or admission_route is None  # admission disabled, always create
            or (self._config.episodes_enabled and self._episode is not None)
        )
        if _should_create_node:
            try:
                await self._ingestion.create_memory_nodes(
                    memory=memory,
                    content=content,
                    all_entities=all_entities,
                    linked_entity_ids=linked_entity_ids,
                    admission_features=admission_features,
                    emit_stage=_emit_stage,
                )
            except Exception:
                logger.warning(
                    "MemoryNode creation failed for %s, continuing", memory.id,
                    exc_info=True,
                )

        logger.info("Stored memory %s: %s", memory.id, content[:80])
        self._event_log.memory_stored(
            memory_id=memory.id,
            content_preview=content,
            memory_type=memory_type,
            domains=memory.domains,
            entity_count=len(all_entities),
            agent_id=source_agent,
        )
        return memory

    # ── Search: Intent Classification ──────────────────────────────────

    async def _classify_search_intent(
        self,
        query: str,
        intent_override: str | None,
        _emit_stage: Callable,
    ) -> IntentResult | None:
        """Classify query intent via exemplar index, keyword fallback, or LLM."""
        if intent_override is not None:
            from ncms.domain.intent import INTENT_TARGETS

            try:
                qi = QueryIntent(intent_override)
            except ValueError:
                valid = [e.value for e in QueryIntent]
                raise ValueError(  # noqa: B904
                    f"Invalid intent '{intent_override}'. Valid intents: {valid}"
                )
            _emit_stage("intent_override", 0.0, {
                "intent": qi.value, "source": "user_override",
            })
            return IntentResult(
                intent=qi, confidence=1.0,
                target_node_types=INTENT_TARGETS.get(qi, ("atomic",)),
            )

        if not self._config.intent_classification_enabled:
            return None

        t0 = time.perf_counter()
        if self._intent_classifier is not None:
            intent_result = self._intent_classifier.classify(query)  # type: ignore[union-attr]
        else:
            intent_result = classify_intent(query)

        llm_fallback_used = False
        if intent_result.confidence < self._config.intent_confidence_threshold:
            if self._config.intent_llm_fallback_enabled:
                from ncms.infrastructure.llm.intent_classifier_llm import (
                    classify_intent_with_llm,
                )
                llm_result = await classify_intent_with_llm(
                    query, model=self._config.llm_model,
                    api_base=self._config.llm_api_base,
                )
                if llm_result is not None:
                    intent_result = llm_result
                    llm_fallback_used = True
                else:
                    _emit_stage("intent_llm_miss", 0, {
                        "query": query[:200],
                        "bm25_intent": intent_result.intent.value,
                        "bm25_confidence": round(intent_result.confidence, 3),
                    })

            if intent_result.confidence < self._config.intent_confidence_threshold:
                _emit_stage("intent_miss", 0, {
                    "query": query[:200],
                    "best_intent": intent_result.intent.value,
                    "best_confidence": round(intent_result.confidence, 3),
                    "llm_attempted": llm_fallback_used,
                })
                intent_result = IntentResult(
                    intent=QueryIntent.FACT_LOOKUP, confidence=1.0,
                    target_node_types=("atomic", "entity_state"),
                )

        _emit_stage("intent_classification", (time.perf_counter() - t0) * 1000, {
            "intent": intent_result.intent.value,
            "confidence": round(intent_result.confidence, 3),
            "target_node_types": list(intent_result.target_node_types),
            "llm_fallback": llm_fallback_used,
        })
        return intent_result


    # ── Search ───────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        domain: str | None = None,
        limit: int = 10,
        agent_id: str | None = None,
        intent_override: str | None = None,
    ) -> list[ScoredMemory]:
        """Execute the full retrieval pipeline: BM25 -> ACT-R rescoring."""
        pipeline_id = uuid.uuid4().hex[:12]
        pipeline_start = time.perf_counter()

        def _emit_stage(
            stage: str, duration_ms: float, data: dict | None = None,
        ) -> None:
            self._event_log.pipeline_stage(
                pipeline_id=pipeline_id, pipeline_type="search", stage=stage,
                duration_ms=duration_ms, data=data, agent_id=agent_id,
            )

        _emit_stage("start", 0.0, {"query": query[:200], "domain": domain, "limit": limit})

        # Phase 4: Intent classification
        intent_result = await self._classify_search_intent(
            query, intent_override, _emit_stage,
        )

        # Phase 4 temporal: parse temporal reference from query
        temporal_ref: TemporalReference | None = None
        if self._config.temporal_enabled:
            t0_temp = time.perf_counter()
            temporal_ref = parse_temporal_reference(query)
            if temporal_ref:
                _emit_stage("temporal_parse", (time.perf_counter() - t0_temp) * 1000, {
                    "range_start": (
                        temporal_ref.range_start.isoformat()
                        if temporal_ref.range_start else None
                    ),
                    "range_end": (
                        temporal_ref.range_end.isoformat()
                        if temporal_ref.range_end else None
                    ),
                    "recency_bias": temporal_ref.recency_bias,
                    "ordinal": temporal_ref.ordinal,
                })

        # Tier 1: Parallel retrieval (BM25 + SPLADE + GLiNER) + RRF fusion
        retrieval = await self._retrieval.retrieve_candidates(
            query, domain, _emit_stage,
        )
        if retrieval is None:
            # No candidates found
            total_ms = (time.perf_counter() - pipeline_start) * 1000
            _emit_stage("complete", total_ms, {
                "result_count": 0, "total_candidates_evaluated": 0,
                "top_score": None, "total_duration_ms": round(total_ms, 2),
            })
            return []
        (
            fused_candidates, bm25_results, splade_results,
            bm25_scores, splade_scores, query_entity_names, parallel_ms,
        ) = retrieval

        # Cross-encoder reranking (selective by intent)
        fused_candidates, ce_scores = (
            await self._retrieval.rerank_candidates(
                query, fused_candidates, intent_result, _emit_stage,
            )
        )

        # Expand candidates: entity resolution → query expansion →
        # graph expansion → node preload → intent supplement
        all_candidates, context_entity_ids, nodes_by_memory = (
            await self._retrieval.expand_candidates(
                query, fused_candidates, query_entity_names,
                intent_result, bm25_scores, parallel_ms, _emit_stage,
            )
        )

        # ── Score, rank, and finalize results ─────────────────────────
        scored = await self._scoring.score_and_rank(
            all_candidates=all_candidates,
            bm25_scores=bm25_scores,
            splade_scores=splade_scores,
            ce_scores=ce_scores,
            context_entity_ids=context_entity_ids,
            nodes_by_memory=nodes_by_memory,
            intent_result=intent_result,
            temporal_ref=temporal_ref,
            domain=domain,
            emit_stage=_emit_stage,
        )

        scored.sort(key=lambda s: s.total_activation, reverse=True)
        results = scored[:limit]

        # Log access ONLY for returned results (not all scored candidates)
        for sm in results:
            await self._store.log_access(
                AccessRecord(
                    memory_id=sm.memory.id,
                    accessing_agent=agent_id,
                    query_context=query,
                )
            )

        # Pipeline complete
        total_ms = (time.perf_counter() - pipeline_start) * 1000
        _emit_stage("complete", total_ms, {
            "result_count": len(results),
            "total_candidates_evaluated": len(scored),
            "top_score": round(results[0].total_activation, 3) if results else None,
            "total_duration_ms": round(total_ms, 2),
        })

        self._event_log.memory_searched(
            query=query,
            result_count=len(results),
            top_score=results[0].total_activation if results else None,
            agent_id=agent_id,
        )

        # Phase 8: Log search for dream cycle PMI computation
        if self._config.dream_cycle_enabled and results:
            try:
                entity_names_for_log = [
                    e["name"] for e in query_entity_names
                ] if query_entity_names else []
                await self._store.log_search(SearchLogEntry(
                    query=query,
                    query_entities=entity_names_for_log,
                    returned_ids=[r.memory.id for r in results],
                    agent_id=agent_id,
                ))
            except Exception:
                logger.debug("Failed to log search for dream cycle", exc_info=True)

        return results

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

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory and remove it from all indexes.

        Returns True if the memory existed and was deleted.
        """
        memory = await self._store.get_memory(memory_id)
        if memory is None:
            return False

        # Remove from search indexes
        import contextlib

        with contextlib.suppress(Exception):
            self._index.remove(memory_id)

        if self._splade is not None:
            with contextlib.suppress(Exception):
                self._splade.remove(memory_id)

        # Remove from persistent store
        await self._store.delete_memory(memory_id)

        return True

    async def delete_memory(self, memory_id: str) -> None:
        self._index.remove(memory_id)
        if self._splade is not None:
            self._splade.remove(memory_id)
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
        return await self._store.count_memories()

    def entity_count(self) -> int:
        return self._graph.entity_count()

    def relationship_count(self) -> int:
        return self._graph.relationship_count()

    # ── Phase 6: Search Feedback & Scale-Aware Flags ───────────────────

    async def record_search_feedback(
        self,
        query: str,
        selected_memory_id: str,
        result_ids: list[str] | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Record implicit feedback: which search result was actually used.

        Logs the selection for future scoring improvements. Also records
        an access event for the selected memory to boost ACT-R base-level.

        Args:
            query: The original search query.
            selected_memory_id: Memory ID the user/agent selected.
            result_ids: Full result set (for position tracking).
            agent_id: Agent that made the selection.
        """
        if not self._config.search_feedback_enabled:
            return

        position = -1
        if result_ids and selected_memory_id in result_ids:
            position = result_ids.index(selected_memory_id)

        # Record access event (boosts ACT-R base-level activation)
        access = AccessRecord(
            memory_id=selected_memory_id,
            accessing_agent=agent_id,
        )
        await self._store.log_access(access)

        # Log for analysis
        self._event_log.emit(DashboardEvent(
            type="search.feedback",
            agent_id=agent_id,
            data={
                "query": query[:200],
                "selected_memory_id": selected_memory_id,
                "position": position,
                "result_count": len(result_ids) if result_ids else 0,
            },
        ))
        logger.info(
            "[feedback] query=%r selected=%s position=%d agent=%s",
            query[:60], selected_memory_id[:8], position, agent_id,
        )

    def check_scale_flags(self) -> dict[str, bool]:
        """Check scale-aware feature flags based on corpus size.

        Returns which features are effectively enabled after scale checks.
        Logs warnings when features are auto-disabled.
        """
        if not self._config.scale_aware_flags_enabled:
            return {
                "reranker": self._config.reranker_enabled,
                "intent": self._config.intent_classification_enabled,
            }

        # Use index size as proxy for corpus size (faster than SQL count)
        try:
            corpus_size = self._index.count() if self._index else 0
        except Exception:
            corpus_size = 0

        flags: dict[str, bool] = {}

        # Reranker: cross-encoder is O(n) per query, expensive at scale
        reranker_ok = corpus_size <= self._config.scale_reranker_max_memories
        flags["reranker"] = self._config.reranker_enabled and reranker_ok
        if self._config.reranker_enabled and not reranker_ok:
            logger.warning(
                "[scale] Reranker auto-disabled: corpus=%d > threshold=%d",
                corpus_size, self._config.scale_reranker_max_memories,
            )

        # Intent classification: exemplar index is fast but scoring adds latency
        intent_ok = corpus_size <= self._config.scale_intent_max_memories
        flags["intent"] = self._config.intent_classification_enabled and intent_ok
        if self._config.intent_classification_enabled and not intent_ok:
            logger.warning(
                "[scale] Intent classification auto-disabled: corpus=%d > threshold=%d",
                corpus_size, self._config.scale_intent_max_memories,
            )

        return flags

    # ── Phase 11: Structured Recall ───────────────────────────────────

    async def recall(
        self,
        query: str,
        domain: str | None = None,
        limit: int = 10,
        agent_id: str | None = None,
    ) -> list[RecallResult]:
        """Structured recall: BM25 search base + intent-based context layering.

        Always starts with the full search() pipeline (BM25+SPLADE+Graph+CE)
        to guarantee recall ≥ search. Then layers intent-specific structured
        results (entity states, episode expansions, causal chains) on top.
        One call returns what currently takes 5+ tool calls.
        """
        # 1. Always run full search pipeline as the base
        scored = await self.search(query, domain=domain, limit=limit)

        # 2. Classify intent for context enrichment strategy
        intent_result: IntentResult | None = None
        if self._config.intent_classification_enabled:
            if self._intent_classifier is not None:
                intent_result = self._intent_classifier.classify(query)
            else:
                intent_result = classify_intent(query)
        intent = intent_result.intent if intent_result else QueryIntent.FACT_LOOKUP

        # 3. Extract entities from query for structured lookups
        from ncms.infrastructure.extraction.gliner_extractor import (
            extract_entities_gliner,
        )

        search_domains = [domain] if domain else []
        cached = await self._get_cached_labels(search_domains)
        labels = resolve_labels(search_domains, cached_labels=cached)
        query_entity_names = extract_entities_gliner(
            query,
            model_name=self._config.gliner_model,
            threshold=self._config.gliner_threshold,
            labels=labels,
            cache_dir=self._config.model_cache_dir,
        )
        context_entity_ids: list[str] = []
        for qe in query_entity_names:
            eid = self._graph.find_entity_by_name(qe["name"])
            if eid:
                context_entity_ids.append(eid)
            else:
                existing = await self._store.find_entity_by_name(qe["name"])
                if existing:
                    context_entity_ids.append(existing.id)

        # 4. Wrap search results as RecallResults (BM25 base — always present)
        seen_memory_ids: set[str] = set()
        base_results: list[RecallResult] = []
        for sm in scored[:limit]:
            seen_memory_ids.add(sm.memory.id)
            base_results.append(
                RecallResult(memory=sm, retrieval_path=intent.value)
            )

        # 5. Layer intent-specific structured results (prepended as bonus)
        bonus_results: list[RecallResult] = []
        if context_entity_ids and intent in (
            QueryIntent.CURRENT_STATE_LOOKUP,
            QueryIntent.HISTORICAL_LOOKUP,
            QueryIntent.CHANGE_DETECTION,
        ):
            bonus_results = await self._enrichment.recall_structured_state(
                context_entity_ids, intent, seen_memory_ids,
            )
        elif intent == QueryIntent.EVENT_RECONSTRUCTION:
            bonus_results = await self._enrichment.recall_episode_bonus(
                scored, seen_memory_ids,
            )

        # 6. Merge: BM25 base first (preserves ranking), then bonus extras
        merged = base_results + bonus_results
        # Cap at limit but always keep all base results
        merged = merged[:max(limit, len(base_results))]

        # 7. Enrich all results with episode, entity state, and causal context
        enriched = await self._enrichment.enrich_existing_results(merged)

        # 8. Expand document profiles into relevant sections
        enriched = await self._enrichment.expand_document_sections(
            enriched, query,
        )

        return enriched

    # ── Phase 5: Level-First Retrieval & Synthesis ─────────────────

    async def search_level(
        self,
        query: str,
        node_types: list[str] | None = None,
        domain: str | None = None,
        limit: int = 10,
        agent_id: str | None = None,
    ) -> list[ScoredMemory]:
        """Level-first retrieval: search scoped to specific HTMG node types.

        Over-fetches from the full search pipeline, then filters to the
        requested node types. Falls back to regular search when level_first
        is disabled or no node_types specified.

        Args:
            query: Search query.
            node_types: Filter to these HTMG types (e.g. ["abstract", "episode"]).
            domain: Optional domain scope.
            limit: Max results to return.
            agent_id: Caller agent ID for access logging.

        Returns:
            Scored memories filtered to requested hierarchy level(s).
        """
        if not node_types or not self._config.level_first_enabled:
            return await self.search(query, domain=domain, limit=limit, agent_id=agent_id)

        # Over-fetch to compensate for post-filter loss
        overfetch = limit * self._config.level_first_overfetch_factor
        candidates = await self.search(query, domain=domain, limit=overfetch, agent_id=agent_id)

        # Filter to requested node types
        filtered: list[ScoredMemory] = []
        for sm in candidates:
            if any(nt in node_types for nt in sm.node_types):
                filtered.append(sm)
                if len(filtered) >= limit:
                    break

        logger.info(
            "[search_level] query=%r node_types=%s overfetch=%d → %d/%d after filter",
            query[:60], node_types, overfetch, len(filtered), len(candidates),
        )
        return filtered

    async def traverse(
        self,
        seed_memory_id: str,
        mode: str = "bottom_up",
        limit: int = 20,
    ) -> TraversalResult:
        """Traverse the HTMG hierarchy from a seed memory.

        Strategies:
        - top_down: Abstract → episodes → atomic fragments
        - bottom_up: Atomic → episode membership → abstract summaries
        - temporal: Entity state timeline for entities in the seed
        - lateral: Episode siblings + related episodes via shared entities

        Args:
            seed_memory_id: Starting memory ID.
            mode: Traversal strategy (top_down, bottom_up, temporal, lateral).
            limit: Max results to collect.

        Returns:
            TraversalResult with ordered results and path metadata.
        """
        traversal_mode = TraversalMode(mode)
        path: list[str] = [seed_memory_id]
        results: list[RecallResult] = []
        levels = 0

        # Get seed memory and its nodes
        seed_memory = await self._store.get_memory(seed_memory_id)
        if seed_memory is None:
            return TraversalResult(
                seed_id=seed_memory_id, traversal_mode=traversal_mode,
            )

        seed_nodes = await self._store.get_memory_nodes_for_memory(seed_memory_id)
        if traversal_mode == TraversalMode.TOP_DOWN:
            results, levels, path = await self._traversal.traverse_top_down(
                seed_memory, seed_nodes, limit,
            )
        elif traversal_mode == TraversalMode.BOTTOM_UP:
            results, levels, path = await self._traversal.traverse_bottom_up(
                seed_memory, seed_nodes, limit,
            )
        elif traversal_mode == TraversalMode.TEMPORAL:
            results, levels, path = await self._traversal.traverse_temporal(
                seed_memory, seed_nodes, limit,
            )
        elif traversal_mode == TraversalMode.LATERAL:
            results, levels, path = await self._traversal.traverse_lateral(
                seed_memory, seed_nodes, limit,
            )

        logger.info(
            "[traverse] seed=%s mode=%s levels=%d results=%d",
            seed_memory_id[:8], mode, levels, len(results),
        )
        return TraversalResult(
            seed_id=seed_memory_id,
            traversal_mode=traversal_mode,
            results=results,
            levels_traversed=levels,
            path=path,
        )

    async def get_topic_map(self) -> list[TopicCluster]:
        """Generate emergent topic map from L4 abstract clustering.

        Delegates to the traversal pipeline, which clusters abstract
        nodes by shared topic_entities using Jaccard overlap.
        """
        return await self._traversal.get_topic_map()

    async def synthesize(
        self,
        query: str,
        mode: str = "summary",
        domain: str | None = None,
        limit: int = 10,
        token_budget: int | None = None,
        traversal: str | None = None,
        seed_memory_id: str | None = None,
    ) -> SynthesizedResponse:
        """Synthesize a structured response from retrieved memories.

        Combines level-first retrieval (or traversal) with LLM synthesis
        to produce token-budgeted responses with source provenance.

        Args:
            query: User query to answer.
            mode: Synthesis mode (summary, detail, timeline, comparison, evidence).
            domain: Optional domain scope.
            limit: Max memories to gather for synthesis.
            token_budget: Max tokens in output (overrides config default).
            traversal: If set, use traversal strategy instead of search.
            seed_memory_id: Required when traversal is set.

        Returns:
            SynthesizedResponse with content, sources, and token accounting.
        """
        if not self._config.synthesis_enabled:
            return SynthesizedResponse(
                query=query, mode=SynthesisMode(mode),
                content="Synthesis is not enabled (NCMS_SYNTHESIS_ENABLED=false).",
            )

        budget = token_budget or self._config.synthesis_token_budget
        synthesis_mode = SynthesisMode(mode)

        # Gather source memories — via traversal or search
        recall_results: list[RecallResult] = []
        traversal_mode = None
        intent_str = "fact_lookup"

        if traversal and seed_memory_id:
            traversal_mode = TraversalMode(traversal)
            trav_result = await self.traverse(
                seed_memory_id, mode=traversal, limit=limit,
            )
            recall_results = trav_result.results
        else:
            # Use recall for enriched context
            recall_results = await self.recall(
                query, domain=domain, limit=limit,
            )
            if recall_results:
                intent_str = recall_results[0].retrieval_path

        if not recall_results:
            return SynthesizedResponse(
                query=query, mode=synthesis_mode,
                content="No relevant memories found for synthesis.",
                token_budget=budget, intent=intent_str,
            )

        # Build context for LLM — truncate to fit budget
        source_ids: list[str] = []
        context_parts: list[str] = []
        # Reserve ~1/4 budget for prompt overhead, use 3/4 for context
        context_char_budget = budget * 3  # ~4 chars per token, 3/4 budget

        for rr in recall_results:
            mem = rr.memory.memory
            # Truncate individual memories proportionally
            max_per = context_char_budget // max(len(recall_results), 1)
            content = mem.content[:max_per]
            source_ids.append(mem.id)

            # Add metadata context
            parts = [f"[{mem.type}] {content}"]
            if rr.context.episode:
                parts.append(f"  Episode: {rr.context.episode.episode_title}")
            if rr.context.entity_states:
                for es in rr.context.entity_states[:3]:
                    parts.append(f"  State: {es.entity_name} = {es.state_value}")
            context_parts.append("\n".join(parts))

            # Check budget
            total_chars = sum(len(p) for p in context_parts)
            if total_chars >= context_char_budget:
                break

        context_text = "\n---\n".join(context_parts)

        # Mode-specific prompt
        mode_instructions = {
            SynthesisMode.SUMMARY: (
                "Provide a concise summary of the key points. "
                "Focus on the most important facts and decisions."
            ),
            SynthesisMode.DETAIL: (
                "Provide a comprehensive, detailed response covering all "
                "relevant information. Include specific details and evidence."
            ),
            SynthesisMode.TIMELINE: (
                "Organize the information chronologically. Present events "
                "and changes in time order with dates where available."
            ),
            SynthesisMode.COMPARISON: (
                "Compare and contrast different perspectives, states, or "
                "time periods. Highlight what changed and why."
            ),
            SynthesisMode.EVIDENCE: (
                "Present fact-backed claims with citations. For each claim, "
                "reference the specific source memory that supports it."
            ),
        }
        default_instruction = mode_instructions[SynthesisMode.SUMMARY]
        instruction = mode_instructions.get(synthesis_mode, default_instruction)

        prompt = (
            f"Based on the following knowledge base memories, answer this query:\n\n"
            f"Query: {query}\n\n"
            f"Instructions: {instruction}\n"
            f"Keep your response under {budget} tokens.\n\n"
            f"Knowledge base context:\n{context_text}"
        )

        # Call LLM
        try:
            from ncms.infrastructure.llm.caller import call_llm_text
            response = await call_llm_text(
                prompt=prompt,
                model=self._config.synthesis_model,
                api_base=self._config.synthesis_api_base,
                max_tokens=budget,
            )
            content = response if response else "Synthesis produced no output."
        except Exception as exc:
            logger.warning("[synthesize] LLM call failed: %s", exc)
            # Fallback: return concatenated snippets
            snippets = []
            for rr in recall_results[:5]:
                snippets.append(rr.memory.memory.content[:200])
            content = (
                "(LLM synthesis unavailable — raw excerpts)\n\n"
                + "\n---\n".join(snippets)
            )

        # Approximate token count (~4 chars per token)
        tokens_used = len(content) // 4

        return SynthesizedResponse(
            query=query,
            mode=synthesis_mode,
            content=content,
            sources=source_ids,
            source_count=len(source_ids),
            token_budget=budget,
            tokens_used=tokens_used,
            traversal=traversal_mode,
            intent=intent_str,
        )

