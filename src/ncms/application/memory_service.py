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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ncms.application.enrichment import EnrichmentPipeline
from ncms.application.entity_extraction_mode import (
    slm_slots_to_entity_dicts,
    use_gliner_entities,
    use_slm_entities,
)
from ncms.application.ingestion import IngestionPipeline
from ncms.application.label_cache import load_cached_labels
from ncms.application.retrieval import RetrievalPipeline
from ncms.application.scoring import ScoringPipeline
from ncms.application.subject import (
    SubjectRegistry,
    bake_subjects_payload,
    inherit_primary_subject_from_parent_doc,
    link_resolved_subject_entities,
    resolve_subjects,
)
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
    Subject,
    SynthesisMode,
    SynthesizedResponse,
    TemporalArithmeticResult,
    TopicCluster,
    TraversalMode,
    TraversalResult,
)
from ncms.domain.protocols import (
    CTLGCueTagger,
    GraphEngine,
    IndexEngine,
    IntentClassifier,
    MemoryStore,
)
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
    "hours": 3600.0,
    "days": 86400.0,
    "weeks": 604800.0,
    "months": 2_629_746.0,  # average month (Gregorian)
    "years": 31_556_952.0,  # average Gregorian year
}


def _format_delta(
    delta_seconds: float,
    requested_unit: str,
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


@dataclass
class _StorePipelineContext:
    """Rolling state for one ``MemoryService.store_memory`` invocation.

    Phase A refactor: ``store_memory`` is now a thin orchestrator
    (CC ≤ 5, A-grade) that builds this context and threads it
    through a sequence of named phase methods.  Each phase reads
    the fields it needs and writes back the fields it owns; no
    long parameter lists, no shared mutable state via closure
    capture.
    """

    # ── Inputs (set at construction; treated as immutable) ─────────
    content: str
    memory_type: str
    importance: float
    source_agent: str | None
    project: str | None
    relationships: list[dict] | None
    observed_at: datetime | None
    subject_legacy: str | None
    subjects_explicit: list[Subject] | None
    parent_doc_id: str | None
    entities_caller: list[dict] | None
    pipeline_id: str
    pipeline_start: float
    emit_stage: Callable[..., None]

    # ── Mutable across phases ─────────────────────────────────────
    domains: list[str] | None = None
    tags: list[str] | None = None
    structured: dict | None = None
    content_hash: str | None = None
    intent_slot_label: Any | None = None
    admission_route: str | None = None
    admission_features: object | None = None
    resolved_subjects: list[Subject] = field(default_factory=list)
    merged_entities: list[dict] = field(default_factory=list)
    slot_entities_present: bool = False


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
        ctlg_cue_tagger: CTLGCueTagger | None = None,
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
        # Dedicated CTLG cue tagger (optional).  This is deliberately
        # separate from intent-slot so CTLG cannot re-enter as a sixth
        # head on the 5-head content SLM.
        self._ctlg_cue_tagger = ctlg_cue_tagger
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

        # Subject canonicalization registry (Phase A — claims A.4 / A.5).
        # Lazy: constructed on first use because the store may not have
        # initialised its sqlite connection at MemoryService construction
        # time.  Holds an aiosqlite connection borrowed from the store;
        # lifecycle is managed by the store, not the registry.  Bound to
        # the same event log so subject.alias_collision events flow into
        # the dashboard's event stream.
        self._subject_registry: SubjectRegistry | None = None

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
        if self._intent_slot is not None:
            features.append("slm")
        if self._ctlg_cue_tagger is not None:
            features.append("ctlg")
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

    def _get_subject_registry(self) -> SubjectRegistry:
        """Lazily construct the subject registry on first use.

        Defers construction until ``store.db`` is guaranteed to exist
        (the store initializes the connection in ``initialize()``,
        which is typically called after ``MemoryService.__init__``).
        """
        if self._subject_registry is None:
            self._subject_registry = SubjectRegistry(
                self._store.db,  # type: ignore[attr-defined]
                event_log=self._event_log,
            )
        return self._subject_registry

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
        subjects: list[Subject] | None = None,
        parent_doc_id: str | None = None,
    ) -> Memory:
        """Store a new memory with automatic indexing and graph updates.

        Phase A refactor: this method is a thin orchestrator (CC ≤ 5).
        Each pipeline phase lives in its own helper method and reads
        the rolling :class:`_StorePipelineContext`.  See
        ``docs/research/phases/phase-a-claims.md`` claims A.2, A.3,
        A.8, A.10, A.17 for the subject-payload contract.

        Subject precedence (claim A.3): caller subjects → caller
        legacy string → SLM ``primary`` role spans → parent-doc
        inheritance (when ``parent_doc_id`` provided) → empty.
        """
        ctx = self._build_store_pipeline_context(
            content=content,
            memory_type=memory_type,
            domains=domains,
            tags=tags,
            source_agent=source_agent,
            project=project,
            structured=structured,
            importance=importance,
            entities=entities,
            relationships=relationships,
            observed_at=observed_at,
            subject=subject,
            subjects=subjects,
            parent_doc_id=parent_doc_id,
        )
        early = await self._run_pre_admission_gates(ctx)
        if early is not None:
            return early
        early = await self._run_slm_extraction(ctx)
        if early is not None:
            return early
        await self._run_ctlg_extraction(ctx)
        await self._resolve_and_bake_subjects(ctx)
        memory = await self._build_and_persist_memory(ctx)
        await self._apply_slm_side_effects(memory, ctx)
        self._compose_entity_set(ctx)
        return await self._dispatch_indexing(memory, ctx)

    # ── Phase A store_memory orchestrator helpers ──────────────────────

    def _build_store_pipeline_context(
        self,
        *,
        content: str,
        memory_type: str,
        domains: list[str] | None,
        tags: list[str] | None,
        source_agent: str | None,
        project: str | None,
        structured: dict | None,
        importance: float,
        entities: list[dict] | None,
        relationships: list[dict] | None,
        observed_at: datetime | None,
        subject: str | None,
        subjects: list[Subject] | None,
        parent_doc_id: str | None,
    ) -> _StorePipelineContext:
        """Construct the pipeline context + bind the stage emitter."""
        pipeline_id = uuid.uuid4().hex[:12]
        pipeline_start = time.perf_counter()

        def _emit_stage(
            stage: str,
            duration_ms: float,
            data: dict | None = None,
            memory_id: str | None = None,
        ) -> None:
            self._event_log.pipeline_stage(
                pipeline_id=pipeline_id,
                pipeline_type="store",
                stage=stage,
                duration_ms=duration_ms,
                data=data,
                agent_id=source_agent,
                memory_id=memory_id,
            )

        _emit_stage(
            "start",
            0.0,
            {"content_preview": content[:120], "memory_type": memory_type},
        )
        return _StorePipelineContext(
            content=content,
            memory_type=memory_type,
            importance=importance,
            source_agent=source_agent,
            project=project,
            relationships=relationships,
            observed_at=observed_at,
            subject_legacy=subject,
            subjects_explicit=subjects,
            parent_doc_id=parent_doc_id,
            entities_caller=entities,
            pipeline_id=pipeline_id,
            pipeline_start=pipeline_start,
            emit_stage=_emit_stage,
            domains=domains,
            tags=tags,
            structured=structured,
        )

    async def _run_pre_admission_gates(
        self,
        ctx: _StorePipelineContext,
    ) -> Memory | None:
        """Dedup + size + classification gates.  Early-exit on hit.

        ``subjects`` and ``parent_doc_id`` are forwarded so the
        navigable / section-service path can plumb them through to
        the recursive ``store_memory`` call that creates the
        document-profile memory (claims A.2 / A.3 / A.10).
        """
        gate_result = await self._ingestion.pre_admission_gates(
            content=ctx.content,
            memory_type=ctx.memory_type,
            importance=ctx.importance,
            tags=ctx.tags,
            structured=ctx.structured,
            source_agent=ctx.source_agent,
            emit_stage=ctx.emit_stage,
            pipeline_start=ctx.pipeline_start,
            subjects=ctx.subjects_explicit,
            parent_doc_id=ctx.parent_doc_id,
        )
        if isinstance(gate_result, Memory):
            return gate_result
        ctx.content_hash, ctx.tags = gate_result
        return None

    async def _run_slm_extraction(
        self,
        ctx: _StorePipelineContext,
    ) -> Memory | None:
        """Run SLM extraction + admission gate.

        Returns a ``Memory`` when admission discards / shunts to
        ephemeral cache (early-exit).  Otherwise unpacks the SLM
        result onto the context and returns ``None``.
        """
        from ncms.application.ingestion.store_helpers import run_slm_and_admission

        slm_result = await run_slm_and_admission(
            config=self._config,
            ingestion=self._ingestion,
            admission=self._admission,
            content=ctx.content,
            domains=ctx.domains,
            tags=ctx.tags,
            source_agent=ctx.source_agent,
            project=ctx.project,
            memory_type=ctx.memory_type,
            importance=ctx.importance,
            structured=ctx.structured,
            emit_stage=ctx.emit_stage,
            pipeline_start=ctx.pipeline_start,
        )
        if isinstance(slm_result, Memory):
            return slm_result
        (
            ctx.intent_slot_label,
            ctx.domains,
            ctx.admission_route,
            ctx.admission_features,
            ctx.structured,
        ) = slm_result
        return None

    async def _run_ctlg_extraction(self, ctx: _StorePipelineContext) -> None:
        """Optionally run CTLG cue tagging on the memory voice.

        Gated by ``config.temporal_enabled`` AND a wired CTLG cue
        tagger.  No-op otherwise.
        """
        if not (self._config.temporal_enabled and self._ctlg_cue_tagger is not None):
            return
        from ncms.application.ctlg import bake_ctlg_payload, extract_ctlg_cues

        domain_hint = (ctx.domains or [""])[0]
        ctlg_result = await extract_ctlg_cues(
            self._ctlg_cue_tagger,
            ctx.content,
            domain=domain_hint,
        )
        if not ctlg_result.tokens:
            return
        ctx.structured = bake_ctlg_payload(
            structured=ctx.structured,
            cue_tags=ctlg_result.tokens,
            method=getattr(self._ctlg_cue_tagger, "name", "ctlg_cue_tagger"),
            latency_ms=ctlg_result.latency_ms,
            voice="memory",
        )
        ctx.emit_stage(
            "ctlg_cues",
            ctlg_result.latency_ms,
            {"n_cue_tags": len(ctlg_result.tokens), "voice": "memory"},
        )

    async def _resolve_and_bake_subjects(
        self,
        ctx: _StorePipelineContext,
    ) -> None:
        """Apply the A.3 precedence chain and bake the payload.

        Implements claims A.2 (bake) + A.3 (precedence + cross-kwarg
        conflict raise) + A.10 (parent-doc inheritance) + A.17 (SLM
        auto-suggest).  Mutation-free on conflict raise.

        Lookup chain for parent-doc inheritance is hash-independent:
        ``parent_doc_id`` → parent Document → profile Memory whose
        ``structured.source_doc_id`` equals ``parent_doc_id`` →
        first ``primary=True`` entry → tagged ``source="document"``.
        """
        t0 = time.perf_counter()
        resolved = await resolve_subjects(
            registry=self._get_subject_registry(),
            config=self._config,
            domains=ctx.domains,
            subject_legacy=ctx.subject_legacy,
            subjects_explicit=ctx.subjects_explicit,
            intent_slot_label=ctx.intent_slot_label,
        )
        if not resolved and ctx.parent_doc_id:
            inherited = await inherit_primary_subject_from_parent_doc(
                store=self._store,
                document_service=self._document_service,
                parent_doc_id=ctx.parent_doc_id,
            )
            if inherited is not None:
                resolved = [inherited]

        ctx.resolved_subjects = resolved
        ctx.structured = bake_subjects_payload(
            subjects=resolved,
            structured=ctx.structured,
        )
        ctx.emit_stage(
            "subjects_resolved",
            (time.perf_counter() - t0) * 1000,
            {
                "n_subjects": len(resolved),
                "sources": sorted({s.source for s in resolved}),
            },
        )

    async def _build_and_persist_memory(
        self,
        ctx: _StorePipelineContext,
    ) -> Memory:
        """Construct the Memory model and persist it to SQLite."""
        memory = Memory(
            content=ctx.content,
            type=cast(Any, ctx.memory_type),
            domains=ctx.domains or [],
            tags=ctx.tags or [],
            source_agent=ctx.source_agent,
            project=ctx.project,
            structured=ctx.structured,
            importance=ctx.importance,
            content_hash=ctx.content_hash,
            observed_at=ctx.observed_at,
        )
        t0 = time.perf_counter()
        await self._store.save_memory(memory)
        ctx.emit_stage(
            "persist",
            (time.perf_counter() - t0) * 1000,
            memory_id=memory.id,
        )
        return memory

    async def _apply_slm_side_effects(
        self,
        memory: Memory,
        ctx: _StorePipelineContext,
    ) -> None:
        """Persist memory_slots + emit dashboard event for the SLM run.

        Runs after ``save_memory`` because the ``memory_slots``
        table has an FK on ``memories(id)``.  Both side-effects
        are wrapped in try/except so a transient backend failure
        doesn't propagate up to the caller — losing the slot rows
        is recoverable; failing the ingest is not.
        """
        if ctx.intent_slot_label is None:
            return
        await self._save_memory_slots_safe(memory.id, ctx.intent_slot_label)
        self._emit_intent_slot_event_safe(memory.id, ctx)

    async def _save_memory_slots_safe(
        self,
        memory_id: str,
        intent_slot_label: Any,
    ) -> None:
        try:
            if hasattr(self._store, "save_memory_slots"):
                await self._store.save_memory_slots(
                    memory_id,
                    slots=intent_slot_label.slots,
                    confidences=intent_slot_label.slot_confidences,
                )
        except Exception:
            logger.warning(
                "[intent_slot] save_memory_slots failed for %s",
                memory_id,
                exc_info=True,
            )

    def _emit_intent_slot_event_safe(
        self,
        memory_id: str,
        ctx: _StorePipelineContext,
    ) -> None:
        try:
            if hasattr(self._event_log, "intent_slot_extracted"):
                self._event_log.intent_slot_extracted(
                    memory_id=memory_id,
                    label=ctx.intent_slot_label,
                    agent_id=ctx.source_agent,
                )
        except Exception:
            logger.debug(
                "[intent_slot] dashboard event emit failed for %s",
                memory_id,
                exc_info=True,
            )

    def _compose_entity_set(self, ctx: _StorePipelineContext) -> None:
        """Merge caller / SLM / subject entities into a single list.

        Three independent sources contribute:
        1. ``ctx.entities_caller`` — caller-provided entity dicts.
        2. SLM slot entities — when ``slm_only`` mode AND the SLM
           emitted slot surface forms above the confidence threshold.
        3. Resolved subjects — every Subject in ``ctx.resolved_subjects``
           gets an entity row (canonical id as name) so MENTIONS_ENTITY
           edges have a target; legacy raw-string entity-link block
           still fires for ``ctx.subject_legacy`` to preserve the
           inline / async parity fitness test.

        Mutates ``ctx.merged_entities`` and ``ctx.slot_entities_present``.
        """
        ctx.merged_entities = list(ctx.entities_caller or [])
        ctx.slot_entities_present = self._merge_slm_slot_entities(ctx)
        self._link_legacy_subject_entity(ctx)
        link_resolved_subject_entities(ctx.merged_entities, ctx.resolved_subjects)

    def _merge_slm_slot_entities(
        self,
        ctx: _StorePipelineContext,
    ) -> bool:
        """Append SLM slot entities; return whether any were added."""
        if not use_slm_entities(self._config):
            return False
        slm_entity_dicts = slm_slots_to_entity_dicts(
            ctx.intent_slot_label,
            confidence_threshold=self._config.slm_confidence_threshold,
        )
        if not slm_entity_dicts:
            return False
        existing = {e["name"].lower() for e in ctx.merged_entities}
        ctx.merged_entities.extend(
            e for e in slm_entity_dicts if e["name"].lower() not in existing
        )
        return True

    @staticmethod
    def _link_legacy_subject_entity(ctx: _StorePipelineContext) -> None:
        """Append the legacy ``subject=`` raw-string entity if absent.

        Phase A note: the legacy raw-string entity-link block stays
        exactly as it was so the inline / async parity fitness test
        still holds.  The multi-subject block downstream skips any
        subject whose id or aliases already match an existing entity,
        so the parity case stays single-entity.
        """
        if not ctx.subject_legacy:
            return
        existing = {e["name"].lower() for e in ctx.merged_entities}
        if ctx.subject_legacy.lower() in existing:
            return
        ctx.merged_entities.append(
            {
                "name": ctx.subject_legacy,
                "type": "subject",
                "attributes": {"source": "caller_subject"},
            },
        )

    async def _dispatch_indexing(
        self,
        memory: Memory,
        ctx: _StorePipelineContext,
    ) -> Memory:
        """Try the async pool first, fall through to inline."""
        from ncms.application.ingestion.store_helpers import (
            finalize_inline_store,
            try_enqueue_indexing,
        )

        enqueued = try_enqueue_indexing(
            index_pool=self._index_pool,
            memory=memory,
            content=ctx.content,
            memory_type=ctx.memory_type,
            domains=ctx.domains,
            tags=ctx.tags,
            source_agent=ctx.source_agent,
            importance=ctx.importance,
            entities=ctx.merged_entities,
            relationships=ctx.relationships,
            admission_features=ctx.admission_features,
            admission_route=ctx.admission_route,
            pipeline_start=ctx.pipeline_start,
            emit_stage=ctx.emit_stage,
            subject=ctx.subject_legacy,
            slot_entities_present=ctx.slot_entities_present,
        )
        if enqueued:
            return memory

        all_entities, linked_entity_ids = await self._ingestion.run_inline_indexing(
            memory=memory,
            content=ctx.content,
            domains=ctx.domains,
            entities_manual=ctx.merged_entities,
            emit_stage=ctx.emit_stage,
            slot_entities_present=ctx.slot_entities_present,
        )
        await finalize_inline_store(
            store=self._store,
            graph=self._graph,
            event_log=self._event_log,
            config=self._config,
            ingestion=self._ingestion,
            episode=self._episode,
            tlg_vocab_cache=self._tlg_vocab_cache,
            memory=memory,
            content=ctx.content,
            memory_type=ctx.memory_type,
            relationships=ctx.relationships,
            all_entities=all_entities,
            linked_entity_ids=linked_entity_ids,
            admission_route=ctx.admission_route,
            admission_features=ctx.admission_features,
            source_agent=ctx.source_agent,
            subject=ctx.subject_legacy,
            pipeline_id=ctx.pipeline_id,
            pipeline_start=ctx.pipeline_start,
            emit_stage=ctx.emit_stage,
        )
        return memory

    # ── Search: Intent Classification ──────────────────────────────────

    # ── Search ───────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        domain: str | None = None,
        limit: int = 10,
        agent_id: str | None = None,
        intent_override: str | None = None,
        reference_time: datetime | None = None,
        stage_candidates_out: dict[str, list[str]] | None = None,
    ) -> list[ScoredMemory]:
        """Execute the full retrieval pipeline: BM25 -> ACT-R rescoring.

        Args:
            reference_time: Overrides "now" for temporal expression
                parsing.  Used when ingesting historical data (e.g.
                conversational sessions from a past date) so "yesterday"
                resolves relative to the conversation's time, not the
                current wall-clock time.  Defaults to ``datetime.now(UTC)``.
            stage_candidates_out: Optional dict that, when provided, is
                populated with per-stage candidate memory ID lists --
                ``bm25``, ``splade``, ``rrf_fused``, ``expanded``,
                ``scored``, ``returned``.  Used by the MSEB harness for
                recall@K-by-stage diagnostics (``gold_in_bm25@50``,
                etc.); production callers leave this ``None`` to avoid
                the per-stage list-allocation overhead.
        """
        pipeline_id = uuid.uuid4().hex[:12]
        pipeline_start = time.perf_counter()

        def _emit_stage(
            stage: str,
            duration_ms: float,
            data: dict | None = None,
        ) -> None:
            self._event_log.pipeline_stage(
                pipeline_id=pipeline_id,
                pipeline_type="search",
                stage=stage,
                duration_ms=duration_ms,
                data=data,
                agent_id=agent_id,
            )

        _emit_stage("start", 0.0, {"query": query[:200], "domain": domain, "limit": limit})

        # Phase 4: Intent classification
        from ncms.application.retrieval.search_helpers import (
            apply_ordinal_if_eligible,
            apply_range_filter_if_eligible,
            classify_search_intent,
            extract_query_range,
        )

        intent_result = await classify_search_intent(
            config=self._config,
            intent_classifier=self._intent_classifier,
            query=query,
            intent_override=intent_override,
            emit_stage=_emit_stage,
        )

        # Phase 4 temporal: parse temporal reference from query
        temporal_ref: TemporalReference | None = None
        if self._config.temporal_enabled:
            t0_temp = time.perf_counter()
            temporal_ref = parse_temporal_reference(
                query,
                now=reference_time,
            )
            if temporal_ref:
                _emit_stage(
                    "temporal_parse",
                    (time.perf_counter() - t0_temp) * 1000,
                    {
                        "range_start": (
                            temporal_ref.range_start.isoformat()
                            if temporal_ref.range_start
                            else None
                        ),
                        "range_end": (
                            temporal_ref.range_end.isoformat() if temporal_ref.range_end else None
                        ),
                        "recency_bias": temporal_ref.recency_bias,
                        "ordinal": temporal_ref.ordinal,
                    },
                )

        # Tier 1: Parallel retrieval (BM25 + SPLADE + configured
        # query entity extraction) + RRF fusion
        retrieval = await self._retrieval.retrieve_candidates(
            query,
            domain,
            _emit_stage,
        )
        if retrieval is None:
            # No candidates found
            total_ms = (time.perf_counter() - pipeline_start) * 1000
            _emit_stage(
                "complete",
                total_ms,
                {
                    "result_count": 0,
                    "total_candidates_evaluated": 0,
                    "top_score": None,
                    "total_duration_ms": round(total_ms, 2),
                },
            )
            return []
        (
            fused_candidates,
            bm25_results,
            splade_results,
            bm25_scores,
            splade_scores,
            query_entity_names,
            parallel_ms,
        ) = retrieval

        # Capture per-stage candidate IDs for opt-in harness
        # diagnostics (gold_in_bm25@50, gold_in_splade@50, etc.).
        # Each stage list is the memory IDs visible AT THAT STAGE
        # before downstream filtering / scoring.  Populated in-place
        # so the caller's dict accumulates as the pipeline runs.
        if stage_candidates_out is not None:
            stage_candidates_out["bm25"] = [mid for mid, _ in bm25_results]
            stage_candidates_out["splade"] = [mid for mid, _ in splade_results]
            stage_candidates_out["rrf_fused"] = [mid for mid, _ in fused_candidates]

        # P1-temporal-experiment: extract the query-side range (Phase
        # A instrumentation ships the log; Phase B.4 uses it below as
        # a hard filter).
        query_entity_names, query_range = extract_query_range(
            config=self._config,
            retrieval=self._retrieval,
            query_entity_names=query_entity_names,
            reference_time=reference_time or datetime.now(UTC),
            emit_stage=_emit_stage,
        )

        # Cross-encoder reranking (selective by intent)
        fused_candidates, ce_scores = await self._retrieval.rerank_candidates(
            query,
            fused_candidates,
            intent_result,
            _emit_stage,
        )

        # Expand candidates: entity resolution → query expansion →
        # graph expansion → node preload → intent supplement
        (
            all_candidates,
            context_entity_ids,
            nodes_by_memory,
        ) = await self._retrieval.expand_candidates(
            query,
            fused_candidates,
            query_entity_names,
            intent_result,
            bm25_scores,
            parallel_ms,
            _emit_stage,
        )
        if stage_candidates_out is not None:
            stage_candidates_out["expanded"] = [mid for mid, _ in all_candidates]

        # P1-temporal-experiment Phase B.4 — explicit-range primitive.
        # When the query has a resolvable calendar range AND temporal
        # intent isn't ARITHMETIC, hard-filter candidates whose
        # persisted content_range doesn't overlap.  Fires before
        # scoring so pruning reduces downstream scoring cost too.
        all_candidates = await apply_range_filter_if_eligible(
            config=self._config,
            retrieval=self._retrieval,
            query=query,
            candidates=all_candidates,
            query_range=query_range,
            temporal_ref=temporal_ref,
            context_entity_ids=context_entity_ids,
            emit_stage=_emit_stage,
        )

        # Phase H.3 — surface query canonicals so the scoring
        # pipeline can match them against per-memory role_spans
        # (only ``role=primary`` matches earn the role-grounding
        # bonus).  Lowercased here so the comparison in
        # :func:`role_grounding_bonus` is a direct membership test.
        query_canonicals: set[str] = {
            qe["name"].lower()
            for qe in query_entity_names
            if isinstance(qe, dict) and isinstance(qe.get("name"), str) and qe["name"]
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
        if stage_candidates_out is not None:
            stage_candidates_out["scored"] = [s.memory.id for s in scored]

        from ncms.application.diagnostics.search_diag import (
            emit_query_diagnostic,
            search_post_score_finalize,
        )

        def _apply_ordinal_bound(q, sc, t_ref, ctx_ids, names, emit):
            return apply_ordinal_if_eligible(
                config=self._config,
                retrieval=self._retrieval,
                query=q,
                scored=sc,
                temporal_ref=t_ref,
                context_entity_ids=ctx_ids,
                subject_names=names,
                emit_stage=emit,
            )

        results, grammar_composed, grammar_confidence = await search_post_score_finalize(
            store=self._store,
            event_log=self._event_log,
            config=self._config,
            apply_ordinal_fn=_apply_ordinal_bound,
            retrieve_lg_fn=self.retrieve_lg,
            query=query,
            limit=limit,
            scored=scored,
            query_entity_names=query_entity_names,
            context_entity_ids=context_entity_ids,
            temporal_ref=temporal_ref,
            agent_id=agent_id,
            pipeline_start=pipeline_start,
            emit_stage=_emit_stage,
            stage_candidates_out=stage_candidates_out,
        )

        # ── Per-query diagnostic ─────────────────────────────────────
        # Always emit (not gated by pipeline_debug).  Defensive: never
        # let diagnostic emission affect search results.
        try:
            await emit_query_diagnostic(
                store=self._store,
                event_log=self._event_log,
                config=self._config,
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
        self,
        name: str,
        entity_type: str,
        attributes: dict | None = None,
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
        self._event_log.emit(
            DashboardEvent(
                type="search.feedback",
                agent_id=agent_id,
                data={
                    "query": query[:200],
                    "selected_memory_id": selected_memory_id,
                    "position": position,
                    "result_count": len(result_ids) if result_ids else 0,
                },
            )
        )
        logger.info(
            "[feedback] query=%r selected=%s position=%d agent=%s",
            query[:60],
            selected_memory_id[:8],
            position,
            agent_id,
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
                corpus_size,
                self._config.scale_reranker_max_memories,
            )

        # Intent classification: exemplar index is fast but scoring adds latency
        intent_ok = corpus_size <= self._config.scale_intent_max_memories
        flags["intent"] = self._config.temporal_enabled and intent_ok
        if self._config.temporal_enabled and not intent_ok:
            logger.warning(
                "[scale] Intent classification auto-disabled: corpus=%d > threshold=%d",
                corpus_size,
                self._config.scale_intent_max_memories,
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
          2. Extract subject entities via the configured query entity
             extraction lane. In ``slm_only`` mode this abstains until
             query-side SLM analysis is wired.
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
        from ncms.application.temporal_arithmetic import (
            extract_anchor_entity_names,
            resolve_anchor_dates,
        )
        from ncms.domain.models import TemporalArithmeticResult
        from ncms.domain.temporal.intent import (
            ARITHMETIC_ANCHOR_COUNTS,
            parse_arithmetic_spec,
        )

        spec = parse_arithmetic_spec(query)
        if spec is None:
            return None

        needed = ARITHMETIC_ANCHOR_COUNTS[spec.operation]
        anchor_names = await extract_anchor_entity_names(
            store=self._store,
            config=self._config,
            query=query,
            domain=domain,
        )
        if len(anchor_names) < needed:
            return None

        ref = reference_time or datetime.now(UTC)
        anchor_dates, anchor_mems = await resolve_anchor_dates(
            store=self._store,
            graph=self._graph,
            index=self._index,
            config=self._config,
            anchor_names=anchor_names[:needed],
            query=query,
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
            query,
            domain=domain,
            limit=limit,
            reference_time=reference_time,
        )

        # 2. Classify intent for context enrichment strategy
        intent_result: IntentResult | None = None
        if self._config.temporal_enabled:
            if self._intent_classifier is not None:
                intent_result = self._intent_classifier.classify(query)
            else:
                intent_result = classify_intent(query)
        intent = intent_result.intent if intent_result else QueryIntent.FACT_LOOKUP

        # 3. Extract entities from query for structured lookups.
        # In ``slm_only`` mode, do not let GLiNER leak into recall
        # context expansion.  Query-side SLM extraction will be added
        # as its own boundary when the adapter proves it can replace
        # the zero-shot query NER path.
        query_entity_names: list[dict[str, Any]] = []
        if use_gliner_entities(self._config):
            from ncms.infrastructure.extraction.gliner_extractor import (
                extract_with_label_budget,
            )

            search_domains = [domain] if domain else []
            cached = await load_cached_labels(self._store, search_domains)
            labels = resolve_labels(search_domains, cached_labels=cached)
            query_entity_names = extract_with_label_budget(
                query,
                labels,
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
            base_results.append(RecallResult(memory=sm, retrieval_path=intent.value))

        # 5. Layer intent-specific structured results (prepended as bonus)
        bonus_results: list[RecallResult] = []
        if context_entity_ids and intent in (
            QueryIntent.CURRENT_STATE_LOOKUP,
            QueryIntent.HISTORICAL_LOOKUP,
            QueryIntent.CHANGE_DETECTION,
        ):
            bonus_results = await self._enrichment.recall_structured_state(
                context_entity_ids,
                intent,
                seen_memory_ids,
            )
        elif intent == QueryIntent.EVENT_RECONSTRUCTION:
            bonus_results = await self._enrichment.recall_episode_bonus(
                scored,
                seen_memory_ids,
            )

        # 6. Merge: BM25 base first (preserves ranking), then bonus extras
        merged = base_results + bonus_results
        # Cap at limit but always keep all base results
        merged = merged[: max(limit, len(base_results))]

        # 7. Enrich all results with episode, entity state, and causal context
        enriched = await self._enrichment.enrich_existing_results(merged)

        # 8. Expand document profiles into relevant sections
        enriched = await self._enrichment.expand_document_sections(
            enriched,
            query,
        )

        return enriched

    # ── TLG: grammar-layer query dispatch (Phase 3c) ───────────────

    async def retrieve_lg(
        self,
        query: str,
        *,
        tlg_query: Any | None = None,
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
        Production callers leave it ``None`` and let the dedicated
        CTLG cue tagger run cue extraction + synthesis.
        """
        from ncms.application.tlg import retrieve_lg as _dispatch
        from ncms.domain.tlg import Confidence, LGIntent, LGTrace
        from ncms.domain.tlg.semantic_parser import SLMQuerySignals, synthesize

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

        # ── CTLG cue tagger + synthesizer ─────────────────────────
        # The dedicated CTLG adapter tags each token with a BIO cue
        # label; the synthesizer composes the tags into a structured
        # :class:`TLGQuery`.  Pass the TLGQuery straight to the
        # dispatcher.
        #
        # When the caller supplied an explicit ``tlg_query``
        # override (test / benchmark path), use it verbatim.
        resolved_tlg_query = tlg_query
        cue_abstained = False
        if resolved_tlg_query is None and self._ctlg_cue_tagger is not None:
            try:
                adapter_domain = (
                    getattr(
                        self._ctlg_cue_tagger,
                        "adapter_domain",
                        None,
                    )
                    or "conversational"
                )
                slm_signals: SLMQuerySignals | None = None
                intent_slot = self._intent_slot
                if intent_slot is not None:
                    try:
                        slm_domain = self._config.default_adapter_domain or adapter_domain
                        loop = asyncio.get_running_loop()
                        slm_label = await loop.run_in_executor(
                            None,
                            lambda: intent_slot.extract(
                                query,
                                domain=slm_domain,
                            ),
                        )
                        slm_signals = SLMQuerySignals.from_label(slm_label)
                    except Exception:  # pragma: no cover — defensive guard
                        logger.debug(
                            "TLG: query-side SLM grounding failed",
                            exc_info=True,
                        )
                from ncms.application.ctlg import extract_ctlg_cues

                ctlg_result = await extract_ctlg_cues(
                    self._ctlg_cue_tagger,
                    query,
                    domain=adapter_domain,
                )
                if ctlg_result.tokens:
                    resolved_tlg_query = synthesize(
                        ctlg_result.tokens,
                        slm_signals=slm_signals,
                    )
                    if resolved_tlg_query is None:
                        # Synthesizer matched no rule — grammar abstains.
                        cue_abstained = True
                else:
                    # CTLG tagger returned no cue signal — abstain.
                    cue_abstained = True
            except Exception:  # pragma: no cover — defensive guard
                logger.debug(
                    "TLG: CTLG cue-tag synthesizer failed",
                    exc_info=True,
                )

        trace = await _dispatch(
            query,
            store=self._store,
            vocabulary_cache=self._tlg_vocab_cache,
            shape_cache=self._tlg_shape_cache,
            tlg_query=resolved_tlg_query,
            cue_abstained=cue_abstained,
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
        marker_counts = {transition: len(heads) for transition, heads in induced.markers.items()}
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
            query[:60],
            node_types,
            overfetch,
            len(filtered),
            len(candidates),
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
                seed_id=seed_memory_id,
                traversal_mode=traversal_mode,
            )

        seed_nodes = await self._store.get_memory_nodes_for_memory(seed_memory_id)
        if traversal_mode == TraversalMode.TOP_DOWN:
            results, levels, path = await self._traversal.traverse_top_down(
                seed_memory,
                seed_nodes,
                limit,
            )
        elif traversal_mode == TraversalMode.BOTTOM_UP:
            results, levels, path = await self._traversal.traverse_bottom_up(
                seed_memory,
                seed_nodes,
                limit,
            )
        elif traversal_mode == TraversalMode.TEMPORAL:
            results, levels, path = await self._traversal.traverse_temporal(
                seed_memory,
                seed_nodes,
                limit,
            )
        elif traversal_mode == TraversalMode.LATERAL:
            results, levels, path = await self._traversal.traverse_lateral(
                seed_memory,
                seed_nodes,
                limit,
            )

        logger.info(
            "[traverse] seed=%s mode=%s levels=%d results=%d",
            seed_memory_id[:8],
            mode,
            levels,
            len(results),
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
                query=query,
                mode=SynthesisMode(mode),
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
                seed_memory_id,
                mode=traversal,
                limit=limit,
            )
            recall_results = trav_result.results
        else:
            # Use recall for enriched context
            recall_results = await self.recall(
                query,
                domain=domain,
                limit=limit,
            )
            if recall_results:
                intent_str = recall_results[0].retrieval_path

        if not recall_results:
            return SynthesizedResponse(
                query=query,
                mode=synthesis_mode,
                content="No relevant memories found for synthesis.",
                token_budget=budget,
                intent=intent_str,
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
            content = "(LLM synthesis unavailable — raw excerpts)\n\n" + "\n---\n".join(snippets)

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
