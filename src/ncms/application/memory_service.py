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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ncms.application.enrichment import EnrichmentPipeline
from ncms.application.ingestion import IngestionPipeline
from ncms.application.label_cache import load_cached_labels
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
    TemporalArithmeticResult,
    TopicCluster,
    TraversalMode,
    TraversalResult,
)
from ncms.domain.protocols import GraphEngine, IndexEngine, IntentClassifier, MemoryStore
from ncms.domain.temporal.parser import (
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
    from ncms.domain.tlg import LGTrace
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.reranking.cross_encoder_reranker import CrossEncoderReranker

logger = logging.getLogger(__name__)


# ── Phase B.5 helpers: arithmetic-delta formatting ─────────────────────
# Kept at module scope so ``MemoryService.compute_temporal_arithmetic``
# stays under the complexity gate.  Pure — no dependencies on service
# state.

_SECONDS_PER_UNIT: dict[str, float] = {
    "hours":  3600.0,
    "days":   86400.0,
    "weeks":  604800.0,
    "months": 2_629_746.0,    # average month (Gregorian)
    "years":  31_556_952.0,   # average Gregorian year
}


def _format_delta(
    delta_seconds: float, requested_unit: str,
) -> tuple[float, str]:
    """Convert a raw delta to (value, unit_label) at the caller's unit.

    Caller's unit is respected (we don't rescale "days" to "weeks"
    even if the delta is small).  Rounded to one decimal place for
    display.
    """
    secs_per = _SECONDS_PER_UNIT.get(requested_unit, _SECONDS_PER_UNIT["days"])
    value = round(delta_seconds / secs_per, 1)
    return value, requested_unit


def _format_answer_text(value: float, unit: str) -> str:
    """Format a numeric delta + unit as a human-readable string.

    Integer-valued deltas drop the decimal; non-integer deltas keep
    one place ("7 days", "3.5 weeks").
    """
    if float(value).is_integer():
        return f"{int(value)} {unit}"
    return f"{value} {unit}"


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
        intent_slot: Any | None = None,
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
        # P2 intent-slot SLM (optional).  Built by the caller so
        # benchmarks can swap adapters without rebuilding the
        # whole MemoryService.
        self._intent_slot = intent_slot
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
            add_entity=self.add_entity,
            intent_slot=self._intent_slot,
        )

        # TLG L1 vocabulary cache — lazy; rebuilt on first use after
        # ingestion.  Always constructed so callers of ``retrieve_lg``
        # don't need to branch on the feature flag; the cache simply
        # returns an empty InducedVocabulary when no ENTITY_STATE
        # nodes exist.
        from ncms.application.tlg import ShapeCacheStore, VocabularyCache
        self._tlg_vocab_cache = VocabularyCache()
        # Persistent skeleton cache (schema v12 ``grammar_shape_cache``).
        # ``warm`` is called lazily on the first retrieve_lg call.
        self._tlg_shape_cache = ShapeCacheStore()
        self._tlg_shape_cache_warmed = False

        # Log active feature flags for diagnostics
        features = []
        if self._config.splade_enabled:
            features.append("SPLADE")
        if self._config.admission_enabled:
            features.append("admission")
        if self._config.temporal_enabled:
            # reconciliation + episodes + intent + tlg + temporal scoring
            features.append("temporal_stack")
        if self._config.slm_enabled:
            features.append("slm")
        if self._config.content_classification_enabled:
            features.append("content_classification")
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
        observed_at: datetime | None = None,
        subject: str | None = None,
    ) -> Memory:
        """Store a new memory with automatic indexing and graph updates.

        When ``subject`` is provided, the caller is asserting the
        entity-subject this memory pertains to (e.g. "adr-0001",
        "ticket-ABC-123", "patient-42").  The ingest pipeline will
        force creation of an L2 ENTITY_STATE node with
        ``metadata["entity_id"] = subject``, bypassing the
        regex / SLM state-change detection fork.  The subject name
        is also linked as an entity so the TLG L1 vocabulary picks
        it up.  Leave as ``None`` for the legacy behaviour where the
        pipeline infers subject from content heuristics.  See
        ``docs/slm-entity-extraction-design.md`` Part 4.
        """
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

        # ── Intent-slot SLM extraction (P2) ───────────────────────────────
        # Runs BEFORE admission + state-change checks so the SLM's
        # admission_head / state_change_head replace the regex paths
        # when confident.  Returns None when the flag is off or no
        # extractor is wired — downstream code treats None as "use
        # legacy regex paths".
        domain_hint = (domains or [""])[0]
        intent_slot_label = await self._ingestion.run_intent_slot_extraction(
            content, domain=domain_hint,
        )
        if intent_slot_label is not None:
            _emit_stage(
                "intent_slot", intent_slot_label.latency_ms,
                {
                    "method": intent_slot_label.method,
                    "intent": intent_slot_label.intent,
                    "topic": intent_slot_label.topic,
                    "admission": intent_slot_label.admission,
                    "state_change": intent_slot_label.state_change,
                    "n_slots": len(intent_slot_label.slots),
                },
            )
            # Auto-populate Memory.domains from the SLM topic head
            # when the operator opted in via slm_populate_domains.
            # This replaces the "user hands us a domain string" flow.
            if (
                intent_slot_label.topic is not None
                and intent_slot_label.is_topic_confident(
                    self._config.slm_confidence_threshold,
                )
                and self._config.slm_populate_domains
            ):
                domains = list(domains or [])
                if intent_slot_label.topic not in domains:
                    domains.append(intent_slot_label.topic)

        # ── Admission scoring — SLM-first, regex fallback ────────────────
        admission_route: str | None = None
        admission_features: object | None = None
        if self._admission is not None and self._config.admission_enabled:
            result = await self._ingestion.gate_admission(
                content=content, domains=domains, tags=tags,
                source_agent=source_agent, project=project,
                memory_type=memory_type, importance=importance,
                structured=structured,
                intent_slot_label=intent_slot_label,
                emit_stage=_emit_stage, pipeline_start=pipeline_start,
            )
            if isinstance(result, Memory):
                return result  # discard or ephemeral — early exit
            admission_route, admission_features, structured = result

        # Bake the SLM outputs into structured BEFORE save_memory so
        # the ``memories`` columns (intent / topic / admission /
        # state_change / intent_slot_method) land in the single INSERT.
        if intent_slot_label is not None:
            structured = dict(structured or {})
            structured["intent_slot"] = {
                "intent": intent_slot_label.intent,
                "intent_confidence": intent_slot_label.intent_confidence,
                "topic": intent_slot_label.topic,
                "topic_confidence": intent_slot_label.topic_confidence,
                "admission": intent_slot_label.admission,
                "admission_confidence": intent_slot_label.admission_confidence,
                "state_change": intent_slot_label.state_change,
                "state_change_confidence": (
                    intent_slot_label.state_change_confidence
                ),
                "method": intent_slot_label.method,
                "latency_ms": intent_slot_label.latency_ms,
                # v7+: role-classified spans.  Thread these through so
                # the L2 ENTITY_STATE builder can source state_value
                # from the primary-role span's canonical form (e.g.
                # ``database=postgresql``) instead of the raw sentence.
                #
                # ``ExtractedLabel.role_spans`` is typed
                # ``list[dict]`` (see domain/models.py) so the adapter
                # boundary already serialised these to dicts via
                # ``_to_domain_label`` in lora_adapter.py.  Pass
                # them through unchanged — earlier code accessed
                # ``r.char_start`` as attribute, which raised
                # ``AttributeError: 'dict' object has no attribute
                # 'char_start'`` whenever the gazetteer detected
                # entities (i.e. on every clinical / software_dev
                # row but no conversational rows since that domain
                # has no gazetteer).
                "role_spans": [
                    dict(r) for r in
                    getattr(intent_slot_label, "role_spans", ()) or ()
                ],
                # Also keep the reconstructed slots dict (primary-role
                # slots + alternative) — the L2 builder checks this as
                # a fallback when role_spans is empty (e.g. pre-v7
                # adapters that only populated ``slots``).
                "slots": dict(getattr(intent_slot_label, "slots", {}) or {}),
                # v8+ CTLG cue tags — per-token BIO labels from the
                # ``shape_cue_head``.  Consumed by the ingest pipeline's
                # ``_extract_and_persist_causal_edges`` to emit
                # CAUSED_BY / ENABLES graph edges.  Empty list on
                # pre-v8 adapters.  Already JSON-ready (list[dict])
                # from both the production and experiment extract()
                # paths — no conversion needed.
                "cue_tags": list(
                    getattr(intent_slot_label, "cue_tags", ()) or ()
                ),
            }

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
            observed_at=observed_at,
        )

        # Persist to SQLite
        t0 = time.perf_counter()
        await self._store.save_memory(memory)
        _emit_stage("persist", (time.perf_counter() - t0) * 1000, memory_id=memory.id)

        # ── Intent-slot side-effects (post-save) ─────────────────────────
        # Slot surface-forms + dashboard event.  Deferred until after
        # save_memory because memory_slots has a FK on memories(id).
        if intent_slot_label is not None:
            try:
                if hasattr(self._store, "save_memory_slots"):
                    await self._store.save_memory_slots(
                        memory.id,
                        slots=intent_slot_label.slots,
                        confidences=intent_slot_label.slot_confidences,
                    )
            except Exception:
                logger.warning(
                    "[intent_slot] save_memory_slots failed for %s",
                    memory.id, exc_info=True,
                )
            try:
                if hasattr(self._event_log, "intent_slot_extracted"):
                    self._event_log.intent_slot_extracted(
                        memory_id=memory.id,
                        label=intent_slot_label,
                        agent_id=source_agent,
                    )
            except Exception:
                logger.debug(
                    "[intent_slot] dashboard event emit failed for %s",
                    memory.id, exc_info=True,
                )

        # ── SLM slot head → entity dicts (Option D' Part 2) ──────────────
        # The slot head is fine-tuned per-domain and outperforms
        # GLiNER's zero-shot NER on trained taxonomies (typed labels,
        # higher precision).  When the slot head produces confident
        # typed entities, we use those AS the entity set and skip
        # GLiNER for this memory — honouring the "SLM primary,
        # GLiNER fallback for open-vocabulary" design invariant.
        #
        # When the slot head produced nothing (empty or sub-threshold),
        # GLiNER runs as the open-vocabulary fallback to ensure the
        # memory is still linked to the graph for retrieval.
        slm_entity_dicts = self._ingestion.slm_slots_to_entity_dicts(
            intent_slot_label,
            confidence_threshold=self._config.slm_confidence_threshold,
        )
        merged_entities = list(entities or [])
        skip_gliner = False
        if slm_entity_dicts:
            existing_names = {e["name"].lower() for e in merged_entities}
            merged_entities.extend(
                e for e in slm_entity_dicts
                if e["name"].lower() not in existing_names
            )
            skip_gliner = True

        # ── Caller-asserted subject (Option D' Part 4) ───────────────────
        # When the caller knows the entity-subject of this memory
        # (MSEB backend, ticket system, patient record, etc.), we
        # link it as a first-class entity so TLG L1 vocabulary
        # induction sees it.  The ENTITY_STATE node is created with
        # ``entity_id = subject`` downstream in create_memory_nodes.
        if subject:
            _subject_lower = subject.lower()
            _existing = {e["name"].lower() for e in merged_entities}
            if _subject_lower not in _existing:
                merged_entities.append({
                    "name": subject,
                    "type": "subject",
                    "attributes": {"source": "caller_subject"},
                })

        # ── Background indexing (fast path) ─────────────────────────────
        # If the async index pool accepts the task, return immediately.
        # Otherwise fall through to inline indexing.
        enqueued = self._try_enqueue_indexing(
            memory=memory, content=content, memory_type=memory_type,
            domains=domains, tags=tags, source_agent=source_agent,
            importance=importance, entities=merged_entities,
            relationships=relationships,
            admission_features=admission_features,
            admission_route=admission_route,
            pipeline_start=pipeline_start, emit_stage=_emit_stage,
            subject=subject,
            skip_gliner=skip_gliner,
        )
        if enqueued:
            return memory

        # ── Inline indexing (fallback / async_indexing disabled) ─────────
        all_entities, linked_entity_ids = (
            await self._ingestion.run_inline_indexing(
                memory=memory, content=content, domains=domains,
                entities_manual=merged_entities, emit_stage=_emit_stage,
                skip_gliner=skip_gliner,
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
            or (self._config.temporal_enabled and self._episode is not None)
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
                    subject=subject,
                )
            except Exception:
                logger.warning(
                    "MemoryNode creation failed for %s, continuing", memory.id,
                    exc_info=True,
                )

            # TLG Phase 3c — ingestion may have produced an L2
            # ENTITY_STATE node (via the state-detection fork inside
            # create_memory_nodes).  Invalidate the L1 vocabulary cache
            # so the next retrieve_lg call rebuilds with the new
            # subject / entity tokens.  Cheap: invalidation is just a
            # None-assignment; rebuild is lazy.
            if self._config.temporal_enabled:
                self._tlg_vocab_cache.invalidate()

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

    def _try_enqueue_indexing(
        self,
        *,
        memory: Memory,
        content: str,
        memory_type: str,
        domains: list[str] | None,
        tags: list[str] | None,
        source_agent: str | None,
        importance: float,
        entities: list[dict] | None,
        relationships: list[dict] | None,
        admission_features: object | None,
        admission_route: str | None,
        pipeline_start: float,
        emit_stage: Callable,
        subject: str | None = None,
        skip_gliner: bool = False,
    ) -> bool:
        """Try to hand indexing off to the background worker pool.

        Returns ``True`` if the task was accepted and the caller
        should return immediately; ``False`` if the caller should
        fall through to inline indexing (pool absent or queue full).
        """
        if self._index_pool is None:
            return False

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
            subject=subject,
            skip_gliner=skip_gliner,
        )
        enqueued = self._index_pool.enqueue(task)  # type: ignore[union-attr]
        if not enqueued:
            logger.warning(
                "Index queue full, falling back to inline for %s",
                memory.id,
            )
            return False

        emit_stage(
            "enqueued",
            (time.perf_counter() - pipeline_start) * 1000,
            {
                "task_id": task.task_id,
                "queue_depth": (
                    self._index_pool.stats().queue_depth  # type: ignore[union-attr]
                ),
            },
            memory_id=memory.id,
        )
        memory.structured = {
            **(memory.structured or {}), "indexing": "queued",
        }
        logger.info(
            "Stored+enqueued memory %s: %s", memory.id, content[:80],
        )
        return True

    # ── Search: Intent Classification ──────────────────────────────────

    def _extract_query_range(
        self,
        query_entity_names: list[dict],
        reference_time: datetime,
        _emit_stage: Callable,
    ) -> tuple[list[dict], object | None]:
        """Split mixed GLiNER output into (entity names, query range).

        Phase A of P1-temporal-experiment — logs the resolved range via
        ``temporal_range_extracted`` but does not filter candidates.
        Returns entity-only list (temporal spans stripped) and the
        resolved range (or None).

        When the feature flag is off, passes through unchanged.
        """
        if not self._config.temporal_range_filter_enabled:
            return query_entity_names, None
        entity_names, temporal_spans = (
            self._retrieval.split_entity_and_temporal_spans(
                query_entity_names,
            )
        )
        if not temporal_spans:
            return entity_names, None
        query_range = self._retrieval.resolve_temporal_range(
            temporal_spans, reference_time,
        )
        _emit_stage("temporal_range_extracted", 0.0, {
            "span_count": len(temporal_spans),
            "spans": [s.text for s in temporal_spans[:10]],
            "range_start": (
                query_range.start.isoformat() if query_range else None
            ),
            "range_end": (
                query_range.end.isoformat() if query_range else None
            ),
            "confidence": (
                query_range.confidence if query_range else None
            ),
        })
        return entity_names, query_range

    def _apply_ordinal_if_eligible(
        self,
        query: str,
        scored: list,
        temporal_ref: object | None,
        context_entity_ids: list[str],
        subject_names: list[str],
        _emit_stage: Callable,
    ) -> list:
        """Classify temporal intent; on ordinal match, reorder by observed_at.

        Phase B.2 wiring.  Gated on ``temporal_range_filter_enabled``
        (the same flag that gates content-date extraction) because the
        ordinal primitive only pays off when the temporal stack is
        active.  No-op otherwise.
        """
        from ncms.domain.temporal.intent import (
            TemporalIntent,
            classify_temporal_intent,
        )

        if not self._config.temporal_range_filter_enabled:
            return scored
        if not scored:
            return scored
        ordinal = (
            getattr(temporal_ref, "ordinal", None)
            if temporal_ref else None
        )
        has_range = bool(temporal_ref) and bool(
            getattr(temporal_ref, "range_start", None)
            or getattr(temporal_ref, "range_end", None),
        )
        has_relative = bool(temporal_ref) and bool(
            getattr(temporal_ref, "recency_bias", False),
        )
        intent = classify_temporal_intent(
            query,
            ordinal=ordinal,
            has_range=has_range,
            has_relative=has_relative,
            subject_count=len(context_entity_ids),
        )
        _emit_stage("temporal_intent_classified", 0.0, {
            "intent": intent.value,
            "subject_count": len(context_entity_ids),
            "ordinal": ordinal,
        })
        if intent == TemporalIntent.ORDINAL_SINGLE and ordinal:
            return self._retrieval.apply_ordinal_ordering(
                scored,
                subject_entity_ids=context_entity_ids,
                subject_names=subject_names,
                ordinal=ordinal,
                multi_subject=False,
            )
        if intent in (
            TemporalIntent.ORDINAL_COMPARE,
            TemporalIntent.ORDINAL_ORDER,
        ) and ordinal:
            return self._retrieval.apply_ordinal_ordering(
                scored,
                subject_entity_ids=context_entity_ids,
                subject_names=None,  # text-fallback unsafe for multi
                ordinal=ordinal,
                multi_subject=True,
            )
        return scored

    async def _apply_range_filter_if_eligible(
        self,
        query: str,
        candidates: list[tuple[str, float]],
        query_range: object | None,
        temporal_ref: object | None,
        context_entity_ids: list[str],
        _emit_stage: Callable,
    ) -> list[tuple[str, float]]:
        """Hard-filter candidates by temporal range when appropriate.

        Phase B.4 dispatch — fires the ``apply_range_filter`` primitive
        when the temporal-intent classifier emits ``RANGE`` or
        ``RELATIVE_ANCHOR``.  Arithmetic queries fast-fail (never
        filter).  ``NONE`` and ordinal intents fall through unchanged
        — the ordinal primitive handles those separately.

        Range source preference:
          1. ``temporal_ref.range_start/range_end`` (regex parser —
             catches common calendar expressions like "during 2024"
             that don't round-trip cleanly through GLiNER).
          2. ``query_range`` (GLiNER + normalizer) — covers the rest.

        Gated on ``temporal_range_filter_enabled`` like the other
        Phase B primitives.
        """
        from ncms.domain.temporal.intent import (
            TemporalIntent,
            classify_temporal_intent,
        )
        from ncms.domain.temporal.normalizer import (
            NormalizedInterval,
            RawSpan,
        )

        if not self._config.temporal_range_filter_enabled:
            return candidates
        if not candidates:
            return candidates

        ordinal = (
            getattr(temporal_ref, "ordinal", None)
            if temporal_ref else None
        )
        has_range = bool(temporal_ref) and bool(
            getattr(temporal_ref, "range_start", None)
            or getattr(temporal_ref, "range_end", None),
        )
        has_relative = bool(temporal_ref) and bool(
            getattr(temporal_ref, "recency_bias", False),
        )
        intent = classify_temporal_intent(
            query,
            ordinal=ordinal,
            has_range=has_range,
            has_relative=has_relative,
            subject_count=len(context_entity_ids),
        )

        if intent not in (
            TemporalIntent.RANGE,
            TemporalIntent.RELATIVE_ANCHOR,
        ):
            return candidates

        # Promote the regex-parser range to a NormalizedInterval if
        # present, else use the normalizer-produced query_range.
        interval: NormalizedInterval | None = None
        if has_range:
            r_start = getattr(temporal_ref, "range_start", None)
            r_end = getattr(temporal_ref, "range_end", None)
            if r_start is not None and r_end is not None:
                interval = NormalizedInterval(
                    start=r_start, end=r_end, confidence=0.9,
                    source_span=RawSpan("<parser>", "date"),
                    origin="parser",
                )
        if interval is None:
            interval = query_range  # may be None
        if interval is None:
            return candidates

        before = len(candidates)
        filtered = await self._retrieval.apply_range_filter(
            candidates, interval,
            missing_range_policy=self._config.temporal_missing_range_policy,
        )
        _emit_stage("temporal_range_filtered", 0.0, {
            "intent": intent.value,
            "candidates_before": before,
            "candidates_after": len(filtered),
            "policy": self._config.temporal_missing_range_policy,
            "range_source": interval.origin,
        })
        return filtered

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

        if not self._config.temporal_enabled:
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
        reference_time: datetime | None = None,
    ) -> list[ScoredMemory]:
        """Execute the full retrieval pipeline: BM25 -> ACT-R rescoring.

        Args:
            reference_time: Overrides "now" for temporal expression
                parsing.  Used when ingesting historical data (e.g.
                conversational sessions from a past date) so "yesterday"
                resolves relative to the conversation's time, not the
                current wall-clock time.  Defaults to ``datetime.now(UTC)``.
        """
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
            temporal_ref = parse_temporal_reference(
                query, now=reference_time,
            )
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

        # P1-temporal-experiment: extract the query-side range (Phase
        # A instrumentation ships the log; Phase B.4 uses it below as
        # a hard filter).
        query_entity_names, query_range = self._extract_query_range(
            query_entity_names,
            reference_time or datetime.now(UTC),
            _emit_stage,
        )

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

        # P1-temporal-experiment Phase B.4 — explicit-range primitive.
        # When the query has a resolvable calendar range AND temporal
        # intent isn't ARITHMETIC, hard-filter candidates whose
        # persisted content_range doesn't overlap.  Fires before
        # scoring so pruning reduces downstream scoring cost too.
        all_candidates = await self._apply_range_filter_if_eligible(
            query, all_candidates, query_range,
            temporal_ref, context_entity_ids, _emit_stage,
        )

        # Phase H.3 — surface query canonicals so the scoring
        # pipeline can match them against per-memory role_spans
        # (only ``role=primary`` matches earn the role-grounding
        # bonus).  Lowercased here so the comparison in
        # :func:`role_grounding_bonus` is a direct membership test.
        query_canonicals: set[str] = {
            qe["name"].lower()
            for qe in query_entity_names
            if isinstance(qe, dict)
            and isinstance(qe.get("name"), str)
            and qe["name"]
        }

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
            query_canonicals=query_canonicals,
        )

        scored.sort(key=lambda s: s.total_activation, reverse=True)

        # P1-temporal-experiment Phase B.2 — ordinal-sequence primitive.
        # Classify temporal intent (pure, fast) and, on an ordinal
        # match with subjects, reorder by ``observed_at`` within the
        # top-K head.  No-op on all other intents.
        subject_names = [
            qe.get("name", "") for qe in query_entity_names
            if isinstance(qe, dict) and qe.get("name")
        ]
        scored = self._apply_ordinal_if_eligible(
            query, scored, temporal_ref,
            context_entity_ids, subject_names, _emit_stage,
        )

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

        # TLG Phase 3c — grammar ∨ BM25 composition.  Runs only when
        # ``NCMS_TEMPORAL_ENABLED=true`` and the grammar dispatch returns a
        # confident answer; otherwise the BM25-derived ranking is
        # returned unchanged (the invariant that guarantees zero
        # confidently-wrong results).
        grammar_composed = False
        grammar_confidence: float | None = None
        if self._config.temporal_enabled:
            results, grammar_composed, grammar_confidence = (
                await self._compose_grammar_with_results(
                    query, results, limit,
                )
            )

        # ── Per-query diagnostic ─────────────────────────────────────
        # Always emit (not gated by pipeline_debug).  See
        # ``EventLog.query_diagnostic`` docstring for payload semantics.
        # Defensive: never let diagnostic emission affect search results.
        try:
            await self._emit_query_diagnostic(
                query=query,
                intent_result=intent_result,
                query_entity_names=query_entity_names,
                context_entity_ids=context_entity_ids,
                temporal_ref=temporal_ref,
                grammar_composed=grammar_composed,
                grammar_confidence=grammar_confidence,
                bm25_count=len(bm25_results),
                splade_count=len(splade_results),
                fused_count=len(fused_candidates),
                expanded_count=len(all_candidates),
                scored=scored,
                results=results,
                total_ms=(time.perf_counter() - pipeline_start) * 1000,
                agent_id=agent_id,
            )
        except Exception:
            logger.debug("query_diagnostic emit failed", exc_info=True)

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
                "intent": self._config.temporal_enabled,
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
        flags["intent"] = self._config.temporal_enabled and intent_ok
        if self._config.temporal_enabled and not intent_ok:
            logger.warning(
                "[scale] Intent classification auto-disabled: corpus=%d > threshold=%d",
                corpus_size, self._config.scale_intent_max_memories,
            )

        return flags

    # ── Phase B.5: Temporal arithmetic resolver ──────────────────────

    async def compute_temporal_arithmetic(
        self,
        query: str,
        reference_time: datetime | None = None,
        domain: str | None = None,
    ) -> TemporalArithmeticResult | None:
        """Compute a deterministic duration answer for an arithmetic
        temporal question — zero LLM.

        Mechanism:
          1. Parse the query for operation (between / since / age_of)
             and unit (days / weeks / months / years / hours).
          2. Extract subject entities via GLiNER (using the same
             label-budget helper the search path uses).
          3. For each anchor entity: resolve to graph entity_id,
             pull the earliest ``observed_at`` among graph-linked
             memories as the anchor date.
          4. Compute the delta in Python, round to the caller's
             requested unit, format an answer string.

        Returns ``None`` (never raises) when:

        * The query isn't an arithmetic temporal question.
        * ``between`` operation needs 2 anchor entities and fewer are
          extracted, OR ``since`` / ``age_of`` needs 1 and none are.
        * An anchor entity has no graph-linked memories, OR those
          memories have no ``observed_at`` timestamps (pre-metadata-
          fallback data).
        * The computed delta is zero or negative after abs().

        This resolver does NOT help LongMemEval Recall@K (the answer
        string isn't a retrievable memory).  Its value is product-
        facing: MCP tools and dashboards can answer arithmetic
        temporal questions without an LLM round-trip.
        """
        from ncms.domain.models import TemporalArithmeticResult
        from ncms.domain.temporal.intent import (
            ARITHMETIC_ANCHOR_COUNTS,
            parse_arithmetic_spec,
        )

        spec = parse_arithmetic_spec(query)
        if spec is None:
            return None

        needed = ARITHMETIC_ANCHOR_COUNTS[spec.operation]
        anchor_names = await self._extract_anchor_entity_names(
            query, domain,
        )
        if len(anchor_names) < needed:
            return None

        ref = reference_time or datetime.now(UTC)
        anchor_dates, anchor_mems = await self._resolve_anchor_dates(
            anchor_names[:needed], query=query,
        )
        if len(anchor_dates) < needed:
            return None

        # Build the two ends of the delta.
        if spec.operation == "between":
            t_a, t_b = anchor_dates[0], anchor_dates[1]
        else:
            # since / age_of: anchor date + reference_time.
            t_a, t_b = anchor_dates[0], ref

        delta_seconds = abs((t_b - t_a).total_seconds())
        if delta_seconds <= 0:
            return None

        value, unit_label = _format_delta(delta_seconds, spec.unit)
        answer_text = _format_answer_text(value, unit_label)
        chron = sorted(
            zip(anchor_mems, anchor_dates, strict=True),
            key=lambda pair: pair[1],
        )
        return TemporalArithmeticResult(
            answer_value=value,
            unit=unit_label,
            answer_text=answer_text,
            operation=spec.operation,
            anchor_memories=[m for m, _ in chron],
            anchor_dates=[d for _, d in chron],
            confidence=1.0 if needed == len(anchor_dates) else 0.7,
        )

    async def _extract_anchor_entity_names(
        self, query: str, domain: str | None,
    ) -> list[str]:
        """GLiNER extraction → subject names, filtered to non-temporal
        entity labels.  Temporal spans are discarded since they aren't
        anchor entities."""
        from ncms.application.retrieval.pipeline import RetrievalPipeline
        from ncms.domain.entity_extraction import (
            add_temporal_labels,
            resolve_labels,
        )
        from ncms.infrastructure.extraction.gliner_extractor import (
            extract_with_label_budget,
        )

        search_domains = [domain] if domain else []
        cached = await load_cached_labels(self._store, search_domains)
        labels = resolve_labels(search_domains, cached_labels=cached)
        if self._config.temporal_range_filter_enabled:
            labels = add_temporal_labels(labels)
        mixed = extract_with_label_budget(
            query, labels,
            model_name=self._config.gliner_model,
            threshold=self._config.gliner_threshold,
            cache_dir=self._config.model_cache_dir,
        )
        entities, _temporal = (
            RetrievalPipeline.split_entity_and_temporal_spans(mixed)
        )
        return [
            str(e.get("name", "")) for e in entities
            if e.get("name")
        ]

    async def _resolve_anchor_dates(
        self,
        anchor_names: list[str],
        query: str | None = None,
    ) -> tuple[list[datetime], list[object]]:
        """For each anchor name, resolve to a representative memory
        mentioning that entity.

        Two-source candidate lookup (same pattern as B.2 ordinal
        primitive):

        1. Graph linkage — ``graph.get_memory_ids_for_entity``.
        2. Content text fallback — any memory whose ``content``
           contains the anchor name (case-insensitive).  Needed
           because GLiNER non-determinism on semantically-similar
           phrasings ("Metropolitan Museum" vs "Metropolitan
           Museum of Art") creates separate graph entities that
           don't share memories.

        Anchor-picking strategy within candidate set:

        * If ``query`` is provided, run BM25 against it and prefer
          the highest-scoring candidate — query qualifiers ("the
          MoMA retrospective") naturally pick the right memory when
          an entity has multiple linked memories.
        * Otherwise, fall back to the earliest ``observed_at``
          candidate.  (Also used when BM25 returns no overlap with
          the anchor candidates.)

        Returns parallel lists of dates and memory objects, in input
        order.  Skips anchors with no resolvable memory.
        """
        bm25_ranking = (
            await self._bm25_anchor_ranking(query) if query else {}
        )
        dates: list[datetime] = []
        memories: list[object] = []
        for name in anchor_names:
            candidates = await self._candidates_for_anchor(name)
            if not candidates:
                continue
            picked = self._pick_anchor_memory(candidates, bm25_ranking)
            if picked is None:
                continue
            memories.append(picked[0])
            dates.append(picked[1])
        return dates, memories

    async def _bm25_anchor_ranking(
        self, query: str,
    ) -> dict[str, float]:
        """Return ``{memory_id: bm25_score}`` for the query.  Used to
        pick the most-relevant anchor memory per entity."""
        try:
            ranked = await asyncio.to_thread(
                self._index.search, query,
                self._config.tier1_candidates,
            )
        except Exception:
            return {}
        return {mid: score for mid, score in ranked}

    @staticmethod
    def _pick_anchor_memory(
        candidates: list[object],
        bm25_scores: dict[str, float],
    ) -> tuple[object, datetime] | None:
        """Choose one memory + its event date from an anchor's candidate set.

        BM25-top preferred; earliest-by-date fallback.
        """
        if bm25_scores:
            ranked = sorted(
                (
                    (mem, bm25_scores.get(mem.id, -1.0))
                    for mem in candidates
                    if bm25_scores.get(mem.id, -1.0) >= 0.0
                ),
                key=lambda pair: pair[1],
                reverse=True,
            )
            if ranked:
                top_mem = ranked[0][0]
                when = (
                    getattr(top_mem, "observed_at", None)
                    or getattr(top_mem, "created_at", None)
                )
                if when is not None:
                    return top_mem, when
        # Fallback: earliest observed_at.
        best: tuple[object, datetime] | None = None
        for mem in candidates:
            when = (
                getattr(mem, "observed_at", None)
                or getattr(mem, "created_at", None)
            )
            if when is None:
                continue
            if best is None or when < best[1]:
                best = (mem, when)
        return best

    async def _candidates_for_anchor(
        self, name: str,
    ) -> list[object]:
        """Gather memories for a given anchor name via graph + text scan."""
        seen_ids: set[str] = set()
        candidates: list[object] = []
        # Graph-linked memories first.
        eid = self._graph.find_entity_by_name(name)
        if eid is None:
            ent = await self._store.find_entity_by_name(name)
            if ent is not None:
                eid = ent.id
        if eid is not None:
            linked_ids = self._graph.get_memory_ids_for_entity(eid)
            if linked_ids:
                batch = await self._store.get_memories_batch(
                    list(linked_ids),
                )
                for mid, mem in batch.items():
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        candidates.append(mem)
        # Text fallback — case-insensitive substring scan.
        if len(name.strip()) >= 3:
            needle = name.strip().lower()
            try:
                all_mems = await self._store.list_memories()
            except Exception:
                all_mems = []
            for mem in all_mems:
                if mem.id in seen_ids:
                    continue
                if needle in (mem.content or "").lower():
                    seen_ids.add(mem.id)
                    candidates.append(mem)
        return candidates

    @staticmethod
    def _earliest_with_observed_at(
        memories: list[object],
    ) -> tuple[object, datetime] | None:
        """Pick the memory with the earliest observed_at (fallback to
        created_at).  Returns None if none of the memories have a
        usable timestamp."""
        best: tuple[object, datetime] | None = None
        for mem in memories:
            when = (
                getattr(mem, "observed_at", None)
                or getattr(mem, "created_at", None)
            )
            if when is None:
                continue
            if best is None or when < best[1]:
                best = (mem, when)
        return best

    # ── Phase 11: Structured Recall ───────────────────────────────────

    async def recall(
        self,
        query: str,
        domain: str | None = None,
        limit: int = 10,
        agent_id: str | None = None,
        reference_time: datetime | None = None,
    ) -> list[RecallResult]:
        """Structured recall: BM25 search base + intent-based context layering.

        Always starts with the full search() pipeline (BM25+SPLADE+Graph+CE)
        to guarantee recall ≥ search. Then layers intent-specific structured
        results (entity states, episode expansions, causal chains) on top.
        One call returns what currently takes 5+ tool calls.

        ``reference_time`` is forwarded to search() for temporal query
        parsing; see ``search()`` for details.
        """
        # 1. Always run full search pipeline as the base
        scored = await self.search(
            query, domain=domain, limit=limit, reference_time=reference_time,
        )

        # 2. Classify intent for context enrichment strategy
        intent_result: IntentResult | None = None
        if self._config.temporal_enabled:
            if self._intent_classifier is not None:
                intent_result = self._intent_classifier.classify(query)
            else:
                intent_result = classify_intent(query)
        intent = intent_result.intent if intent_result else QueryIntent.FACT_LOOKUP

        # 3. Extract entities from query for structured lookups
        from ncms.infrastructure.extraction.gliner_extractor import (
            extract_with_label_budget,
        )

        search_domains = [domain] if domain else []
        cached = await load_cached_labels(self._store, search_domains)
        labels = resolve_labels(search_domains, cached_labels=cached)
        query_entity_names = extract_with_label_budget(
            query, labels,
            model_name=self._config.gliner_model,
            threshold=self._config.gliner_threshold,
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

    # ── TLG: grammar-layer query dispatch (Phase 3c) ───────────────

    async def retrieve_lg(
        self, query: str, *, tlg_query: Any | None = None,
    ) -> LGTrace:
        """Dispatch ``query`` through the TLG grammar layer.

        Returns an :class:`~ncms.domain.tlg.LGTrace` with a
        ``confidence`` and optional ``grammar_answer``.  Callers
        compose with their existing BM25 ranking via
        :func:`ncms.domain.tlg.compose` — only HIGH / MEDIUM
        answers are prepended; other confidence levels leave BM25
        unchanged (the ``grammar ∨ BM25`` invariant).

        Safe to call on any store state.  Returns a
        :attr:`Confidence.NONE` trace when TLG is disabled via
        ``NCMSConfig.temporal_enabled`` — this keeps callers'
        composition logic identical whether TLG is on or off.

        ``tlg_query`` is a test / benchmark hatch — when set,
        it overrides the cue-head + synthesizer step and hands the
        composed :class:`TLGQuery` directly to the dispatcher.
        Production callers leave it ``None`` and let the
        ingest-time SLM chain run the cue head + synthesizer.
        """
        from ncms.application.tlg import retrieve_lg as _dispatch
        from ncms.domain.tlg import Confidence, LGIntent, LGTrace
        from ncms.domain.tlg.cue_taxonomy import TaggedToken
        from ncms.domain.tlg.semantic_parser import synthesize

        if not self._config.temporal_enabled:
            return LGTrace(
                query=query,
                intent=LGIntent(kind=""),
                confidence=Confidence.NONE,
                proof="tlg disabled (NCMS_TEMPORAL_ENABLED=false)",
            )
        # Warm the persistent shape cache once per process lifetime.
        if not self._tlg_shape_cache_warmed:
            try:
                await self._tlg_shape_cache.warm(self._store)
            except Exception:  # pragma: no cover — defensive guard
                logger.debug("TLG shape-cache warm failed", exc_info=True)
            self._tlg_shape_cache_warmed = True

        # ── CTLG cue head + synthesizer (v8+) ─────────────────────
        # The SLM's ``shape_cue_head`` tags each token with a BIO
        # cue label; the synthesizer composes the tags into a
        # structured :class:`TLGQuery`.  Pass the TLGQuery straight
        # to the dispatcher.
        #
        # When the caller supplied an explicit ``tlg_query``
        # override (test / benchmark path), use it verbatim.
        resolved_tlg_query = tlg_query
        slm_abstained = False
        if resolved_tlg_query is None and self._intent_slot is not None:
            try:
                adapter_domain = getattr(
                    self._intent_slot, "adapter_domain", None,
                ) or "conversational"
                slm_result = self._intent_slot.extract(
                    query, domain=adapter_domain,
                )
                cue_tag_dicts = list(getattr(slm_result, "cue_tags", ()) or ())
                if cue_tag_dicts:
                    # Convert list[dict] boundary type back to
                    # TaggedToken dataclasses for the synthesizer.
                    tokens = [
                        TaggedToken(
                            char_start=int(d["char_start"]),
                            char_end=int(d["char_end"]),
                            surface=str(d["surface"]),
                            cue_label=str(d["cue_label"]),
                            confidence=float(d.get("confidence", 1.0)),
                        )
                        for d in cue_tag_dicts
                    ]
                    resolved_tlg_query = synthesize(tokens)
                    if resolved_tlg_query is None:
                        # Synthesizer matched no rule — grammar abstains.
                        slm_abstained = True
                else:
                    # Adapter ships no cue head (pre-v8) — abstain.
                    slm_abstained = True
            except Exception:  # pragma: no cover — defensive guard
                logger.debug(
                    "TLG: cue-head synthesizer failed", exc_info=True,
                )

        trace = await _dispatch(
            query,
            store=self._store,
            vocabulary_cache=self._tlg_vocab_cache,
            shape_cache=self._tlg_shape_cache,
            tlg_query=resolved_tlg_query,
            slm_abstained=slm_abstained,
        )
        # Dashboard observability — one event per dispatch; dashboards
        # can aggregate the ``grammar.*`` namespace to visualise intent
        # mix + confidence distribution.
        with contextlib.suppress(Exception):  # pragma: no cover — defensive guard
            self._event_log.grammar_dispatched(  # type: ignore[attr-defined]
                query=query,
                intent=trace.intent.kind,
                subject=trace.intent.subject,
                entity=trace.intent.entity,
                confidence=trace.confidence.value,
                grammar_answer=trace.grammar_answer,
                proof=trace.proof,
            )
        return trace

    def invalidate_tlg_vocabulary(self) -> None:
        """Clear the L1 vocabulary cache so the next
        :meth:`retrieve_lg` call rebuilds.

        Call after bulk ingestion or a maintenance pass that changes
        which ENTITY_STATE nodes exist — otherwise the cache keeps
        serving the pre-change vocabulary.
        """
        self._tlg_vocab_cache.invalidate()

    async def run_tlg_induction_pass(self) -> dict[str, Any]:
        """Run a full L2 marker induction + L1 vocabulary rebuild.

        Called by the maintenance scheduler (or manually from the
        CLI / MCP tool).  Returns a small summary dict for logging.
        No-op when TLG is disabled — returns an empty summary.
        """
        if not self._config.temporal_enabled:
            return {"status": "skipped", "reason": "tlg_disabled"}
        from ncms.application.tlg import induce_and_persist_markers
        induced = await induce_and_persist_markers(self._store)
        # Rebuild L1 vocabulary so the next retrieve_lg picks up the
        # current corpus state (no need to wait for an ingest event).
        self.invalidate_tlg_vocabulary()
        await self._tlg_vocab_cache.get_vocabulary(self._store)
        marker_counts = {
            transition: len(heads)
            for transition, heads in induced.markers.items()
        }
        logger.info(
            "TLG induction pass: %d transition buckets, counts=%s",
            len(induced.markers),
            marker_counts,
        )
        return {
            "status": "ok",
            "transitions": len(induced.markers),
            "marker_counts": marker_counts,
        }

    async def _emit_query_diagnostic(
        self,
        *,
        query: str,
        intent_result: object | None,
        query_entity_names: list[dict],
        context_entity_ids: list[str],
        temporal_ref: object | None,
        grammar_composed: bool,
        grammar_confidence: float | None,
        bm25_count: int,
        splade_count: int,
        fused_count: int,
        expanded_count: int,
        scored: list[ScoredMemory],
        results: list[ScoredMemory],
        total_ms: float,
        agent_id: str | None,
    ) -> None:
        """Build and emit the comprehensive per-query diagnostic.

        See :meth:`EventLog.query_diagnostic` for the payload spec.
        Always-on (not gated by ``pipeline_debug``); the user wants
        visibility on every query so retiring the regex/heuristic
        fallbacks (Phase I.2-I.6) can be verified-by-observation
        rather than verified-by-rerunning-MSEB.

        Designed to be CTLG-extensible: when the cue-tagger ships,
        its ``cue_tags_count`` slots into ``signal_coverage`` and the
        ``causal_edges`` field of ``htmg_subject_stats`` becomes
        non-trivial.
        """
        # Signal coverage — how many candidates had a non-zero
        # contribution from each retrieval signal.  Tells operators
        # which heads are firing on this query's candidate set.
        coverage = {
            "intent_alignment": sum(
                1 for s in scored if s.intent_alignment_contrib != 0.0
            ),
            "state_change_alignment": sum(
                1 for s in scored
                if s.state_change_alignment_contrib != 0.0
            ),
            "role_grounding": sum(
                1 for s in scored if s.role_grounding_contrib != 0.0
            ),
            "hierarchy_bonus": sum(
                1 for s in scored if s.hierarchy_bonus != 0.0
            ),
            "temporal": sum(
                1 for s in scored if s.temporal_score != 0.0
            ),
            "graph": sum(1 for s in scored if s.spreading != 0.0),
            "reconciliation_penalty": sum(
                1 for s in scored if s.reconciliation_penalty != 0.0
            ),
        }

        # HTMG subject stats — for each resolved entity ID, count
        # the L2 / supersession / causal edges in its neighborhood.
        # Helpful for "why didn't the gold answer surface?" debugging
        # when the corpus has rich state evolution.  Skip when
        # temporal disabled (no L2 path active anyway).
        htmg_stats: dict[str, int] = {}
        if self._config.temporal_enabled and context_entity_ids:
            try:
                l2_count = 0
                sup_count = 0
                causal_count = 0
                for eid in context_entity_ids[:10]:  # cap I/O
                    states = (
                        await self._store.get_entity_states_by_entity(
                            eid,
                        )
                    )
                    l2_count += len(states)
                    for s in states:
                        if not s.is_current:
                            sup_count += 1
                htmg_stats = {
                    "l2_entity_states": l2_count,
                    "supersession_chain_size": sup_count,
                    "causal_edges": causal_count,  # CTLG fills this
                }
            except Exception:
                logger.debug(
                    "htmg_subject_stats lookup failed", exc_info=True,
                )

        # Top-result signal breakdown — full vector for the rank-1
        # result so operators can see exactly which signals moved it
        # there.  Includes raw + post-weight fields where available.
        top_breakdown: dict[str, object] | None = None
        if results:
            top = results[0]
            top_breakdown = {
                "memory_id": top.memory.id,
                "content_preview": top.memory.content[:120],
                "node_types": top.node_types,
                "bm25_raw": round(top.bm25_score, 3),
                "splade_raw": round(top.splade_score, 3),
                "graph_raw": round(top.spreading, 3),
                "h_bonus": round(top.hierarchy_bonus, 3),
                "ia_contrib": round(top.intent_alignment_contrib, 3),
                "sc_contrib": round(
                    top.state_change_alignment_contrib, 3,
                ),
                "rg_contrib": round(top.role_grounding_contrib, 3),
                "temporal": round(top.temporal_score, 3),
                "penalty": round(top.reconciliation_penalty, 3),
                "total": round(top.total_activation, 3),
                "is_superseded": top.is_superseded,
                "has_conflicts": top.has_conflicts,
            }

        intent_str: str | None = None
        intent_conf: float | None = None
        if intent_result is not None:
            intent_str = getattr(
                getattr(intent_result, "intent", None), "value", None,
            )
            intent_conf = getattr(intent_result, "confidence", None)

        temporal_ref_str: str | None = None
        if temporal_ref is not None:
            temporal_ref_str = repr(temporal_ref)[:200]

        query_names = [
            qe["name"] for qe in query_entity_names
            if isinstance(qe, dict) and qe.get("name")
        ]

        self._event_log.query_diagnostic(
            query=query,
            intent=intent_str,
            intent_confidence=intent_conf,
            query_entities=query_names,
            resolved_entity_ids=list(context_entity_ids),
            temporal_ref=temporal_ref_str,
            grammar_composed=grammar_composed,
            grammar_confidence=grammar_confidence,
            candidate_counts={
                "bm25": bm25_count,
                "splade": splade_count,
                "rrf_fused": fused_count,
                "expanded": expanded_count,
                "scored": len(scored),
                "returned": len(results),
            },
            signal_coverage=coverage,
            htmg_subject_stats=htmg_stats,
            top_breakdown=top_breakdown,
            result_count=len(results),
            total_ms=total_ms,
            agent_id=agent_id,
        )

        # One-line INFO log for grep-ability.  Format is stable so
        # downstream tooling (CTLG verification harness, dashboards)
        # can parse it.  Compact: [diag] q="..." intent=... ents=N
        # cnt=B/S/F/E/Sc/R sigcov=ia,sc,rg,h,t,g,pen top=mid:total
        sig_compact = "/".join(
            str(coverage[k]) for k in (
                "intent_alignment",
                "state_change_alignment",
                "role_grounding",
                "hierarchy_bonus",
                "temporal",
                "graph",
                "reconciliation_penalty",
            )
        )
        top_compact = (
            f"{top_breakdown['memory_id']}:{top_breakdown['total']}"
            if top_breakdown else "none"
        )
        logger.info(
            "[diag] q=%r intent=%s/%s ents=%d cnt=%d/%d/%d/%d/%d/%d "
            "sigcov=%s gram=%s top=%s ms=%.1f",
            query[:80], intent_str, intent_conf,
            len(query_names),
            bm25_count, splade_count, fused_count,
            expanded_count, len(scored), len(results),
            sig_compact,
            f"y@{grammar_confidence:.2f}" if grammar_composed else "n",
            top_compact, total_ms,
        )

    async def _compose_grammar_with_results(
        self,
        query: str,
        results: list[ScoredMemory],
        limit: int,
    ) -> tuple[list[ScoredMemory], bool, float | None]:
        """Apply the grammar ∨ BM25 invariant to ``search`` results.

        Runs ``retrieve_lg`` and, when the trace is confident, moves
        the grammar answer's backing Memory to rank 1 — preserving
        every other score field.  Zone-context siblings follow.
        When the trace is not confident, the input list is returned
        unchanged.

        Never raises: any failure in dispatch or resolution logs and
        returns the original list.  Benchmarks / callers observe
        strict graceful-degradation semantics — TLG can only improve
        results, not break search.

        Returns a tuple of ``(results, did_compose, confidence)``:
          * ``results`` — the (possibly reordered) result list.
          * ``did_compose`` — True iff the grammar trace was
            confident enough to displace the BM25 top-1.
          * ``confidence`` — the trace's confidence value when
            composition fired, otherwise ``None``.

        The tuple feeds the per-query ``query_diagnostic`` event so
        operators can see whether grammar composition actually
        contributed to a given query's ranking.
        """
        try:
            trace = await self.retrieve_lg(query)
        except Exception:
            logger.warning("TLG dispatch failed during search", exc_info=True)
            return results, False, None
        if not trace.has_confident_answer():
            return results, False, None

        # grammar_answer is a MemoryNode ID — resolve it to the
        # backing Memory.  Zone context IDs get the same treatment.
        composed = await self._compose_trace_onto_scored(
            trace.grammar_answer, trace.zone_context, results,
        )
        composed = composed[:limit] if limit else composed
        try:
            # Grammar-answer's backing memory_id for dashboard observers.
            grammar_memory_id = await self._resolve_node_to_memory_id(
                trace.grammar_answer,
            )
            self._event_log.grammar_composed(  # type: ignore[attr-defined]
                query=query,
                intent=trace.intent.kind,
                confidence=trace.confidence.value,
                grammar_answer_memory_id=grammar_memory_id,
                zone_context_count=len(trace.zone_context),
                bm25_count_before=len(results),
                composed_count=len(composed),
            )
        except Exception:  # pragma: no cover — defensive guard
            pass
        return composed, True, trace.confidence.value

    async def _compose_trace_onto_scored(
        self,
        grammar_answer: str | None,
        zone_context: list[str],
        results: list[ScoredMemory],
    ) -> list[ScoredMemory]:
        """Reorder ``results`` so grammar answer + zone context lead.

        Scores are preserved on items already in the list; new items
        fetched from the store get a sentinel score so callers
        sorting by total_activation keep them at the top.
        """
        by_memory_id = {sm.memory.id: sm for sm in results}
        grammar_memory_id = await self._resolve_node_to_memory_id(grammar_answer)
        zone_memory_ids: list[str] = []
        for node_id in zone_context:
            mid = await self._resolve_node_to_memory_id(node_id)
            if mid is not None:
                zone_memory_ids.append(mid)

        # Compute the top-rank sentinel so composed items sort above
        # everything BM25 produced.  Preserves relative order among
        # composed items.
        max_activation = max(
            (sm.total_activation for sm in results), default=0.0,
        )
        sentinel = max_activation + 1.0

        composed: list[ScoredMemory] = []
        placed: set[str] = set()

        async def _emit(memory_id: str | None) -> None:
            if memory_id is None or memory_id in placed:
                return
            existing = by_memory_id.get(memory_id)
            if existing is not None:
                # Clone with bumped activation so sorted-by-score
                # downstream stays stable.
                bumped = existing.model_copy(
                    update={"total_activation": sentinel}
                )
                composed.append(bumped)
            else:
                mem = await self._store.get_memory(memory_id)
                if mem is None:
                    return
                composed.append(ScoredMemory(
                    memory=mem, total_activation=sentinel,
                ))
            placed.add(memory_id)

        await _emit(grammar_memory_id)
        for mid in zone_memory_ids:
            await _emit(mid)

        for sm in results:
            if sm.memory.id not in placed:
                composed.append(sm)
                placed.add(sm.memory.id)
        return composed

    async def _resolve_node_to_memory_id(
        self, node_id: str | None,
    ) -> str | None:
        """Map a MemoryNode ID (grammar_answer / zone_context) to the
        backing Memory ID.  Returns ``None`` when the node doesn't
        exist or has no ``memory_id`` FK (shouldn't happen in prod
        but we stay defensive)."""
        if node_id is None:
            return None
        try:
            node = await self._store.get_memory_node(node_id)
        except Exception:
            logger.debug(
                "TLG: failed to fetch memory node %s", node_id, exc_info=True,
            )
            return None
        return node.memory_id if node is not None else None

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

