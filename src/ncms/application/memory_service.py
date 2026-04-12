"""Memory Service - orchestrates storage, indexing, graph, and scoring.

This is the primary entry point for memory operations:
store, search, recall, and manage the full retrieval pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import re
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from ncms.config import NCMSConfig
from ncms.domain.entity_extraction import resolve_labels
from ncms.domain.intent import IntentResult, QueryIntent, classify_intent
from ncms.domain.models import (
    AccessRecord,
    DocumentSectionContext,
    EdgeType,
    Entity,
    EntityStateSnapshot,
    EpisodeContext,
    EpisodeMeta,
    Memory,
    RecallContext,
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
from ncms.domain.scoring import (
    activation_noise,
    base_level_activation,
    conflict_annotation_penalty,
    graph_spreading_activation,
    hierarchy_match_bonus,
    ppr_graph_score,
    recency_score,
    retrieval_probability,
    spreading_activation,
    supersession_penalty,
    total_activation,
)
from ncms.domain.temporal_parser import (
    TemporalReference,
    compute_temporal_proximity,
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

    # ── Entity State Extraction (Phase 2A) ───────────────────────────────

    @staticmethod
    def _extract_entity_state_meta(
        content: str, entities: list[dict],
    ) -> dict:
        """Extract entity state metadata from content and extracted entities.

        Heuristic patterns (tried in order):
        1. "EntityName: key = value" — structured assignment
        2. "EntityName key changed/updated from X to Y" — state transition
        3. "EntityName: key changed/updated from X to Y" — colon + transition
        4. "EntityName key is/was/set to value" — state declaration
        5. Fallback: first GLiNER entity as entity_id, content as value

        Returns a dict suitable for MemoryNode.metadata with entity_id, state_key,
        state_value, and optionally state_scope.
        """
        import re

        # Pattern 1: "EntityName: key = value"
        # e.g. "auth-service: status = deployed"
        p_assign = re.compile(
            r"^([a-zA-Z0-9_\-]+)\s*:\s*([a-zA-Z0-9_\-]+)\s*=\s*(.+)$",
            re.MULTILINE,
        )
        m = p_assign.search(content)
        if m:
            return {
                "entity_id": m.group(1).strip(),
                "state_key": m.group(2).strip(),
                "state_value": m.group(3).strip(),
            }

        # Pattern 2: "EntityName key changed/updated from X to Y"
        # e.g. "auth-service status changed from healthy to degraded ..."
        p_transition = re.compile(
            r"^([a-zA-Z0-9_\-]+)\s+([a-zA-Z0-9_\-]+)\s+"
            r"(?:changed|updated)\s+from\s+(.+?)\s+to\s+(.+?)(?:\s+(?:due|for|after|because)\b.*)?$",
            re.MULTILINE | re.IGNORECASE,
        )
        m = p_transition.search(content)
        if m:
            return {
                "entity_id": m.group(1).strip(),
                "state_key": m.group(2).strip(),
                "state_value": m.group(4).strip(),
                "state_previous": m.group(3).strip(),
            }

        # Pattern 3: "EntityName: key changed/updated from X to Y"
        # e.g. "rate-limiter: state changed from 100 req/min to 200 req/min ..."
        p_colon_transition = re.compile(
            r"^([a-zA-Z0-9_\-]+)\s*:\s*([a-zA-Z0-9_\-]+)\s+"
            r"(?:changed|updated)\s+from\s+(.+?)\s+to\s+(.+?)(?:\s+(?:due|for|after|because|per)\b.*)?$",
            re.MULTILINE | re.IGNORECASE,
        )
        m = p_colon_transition.search(content)
        if m:
            return {
                "entity_id": m.group(1).strip(),
                "state_key": m.group(2).strip(),
                "state_value": m.group(4).strip(),
                "state_previous": m.group(3).strip(),
            }

        # Pattern 4: "EntityName key is/are/was/were/changed to/set to value"
        # e.g. "auth-service status is deployed"
        p_declaration = re.compile(
            r"^([a-zA-Z0-9_\-]+)\s+([a-zA-Z0-9_\-]+)\s+"
            r"(?:is|are|was|were|changed to|updated to|set to)\s+(.+)$",
            re.MULTILINE | re.IGNORECASE,
        )
        m = p_declaration.search(content)
        if m:
            return {
                "entity_id": m.group(1).strip(),
                "state_key": m.group(2).strip(),
                "state_value": m.group(3).strip(),
            }

        # Pattern 5: Markdown "## Status\n\nvalue" (e.g. ADR documents)
        # The heading title (# Title) provides entity context.
        p_md_status = re.compile(
            r"^#\s+(.+?)$.*?^##?\s*[Ss]tatus\s*$\s*^(\w[\w\s]*)$",
            re.MULTILINE | re.DOTALL,
        )
        m = p_md_status.search(content)
        if m and entities:
            title = m.group(1).strip()
            status_val = m.group(2).strip()
            # Find best matching entity from GLiNER extraction
            entity_id = entities[0]["name"]
            title_lower = title.lower()
            for ent in entities:
                if ent["name"].lower() in title_lower:
                    entity_id = ent["name"]
                    break
            return {
                "entity_id": entity_id,
                "state_key": "status",
                "state_value": status_val,
            }

        # Pattern 6: YAML "status: value" (e.g. security checklists, config)
        p_yaml_status = re.compile(
            r"^\s*status\s*:\s*(\w[\w_\-]*)",
            re.MULTILINE | re.IGNORECASE,
        )
        m = p_yaml_status.search(content)
        if m and entities:
            return {
                "entity_id": entities[0]["name"],
                "state_key": "status",
                "state_value": m.group(1).strip(),
            }

        # Fallback: use first entity as entity_id.
        # Use the first line containing an assignment-like pattern as the
        # state_value (not a 500-char prefix of full content).
        if entities:
            # Try to find the most relevant line
            best_line = ""
            for line in content.splitlines():
                stripped = line.strip()
                if stripped and any(
                    c in stripped for c in ("=", ":", "→")
                ):
                    best_line = stripped[:200]
                    break
            if not best_line:
                best_line = content.splitlines()[0].strip()[:200] if content else ""
            return {
                "entity_id": entities[0]["name"],
                "state_key": "state",
                "state_value": best_line,
            }

        return {}

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

        # ── Gate 1: Content-hash dedup ───────────────────────────────────
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        try:
            existing = await self._store.get_memory_by_content_hash(content_hash)
        except AttributeError:
            existing = None  # Store doesn't support content_hash lookup yet
        if existing is not None:
            logger.info(
                "Dedup: content hash %s already exists as memory %s",
                content_hash[:12], existing.id,
            )
            _emit_stage("dedup_skip", (time.perf_counter() - pipeline_start) * 1000, {
                "existing_memory_id": existing.id,
                "content_hash": content_hash[:12],
            })
            return existing

        # ── Gate 2: Content size diagnostic ──────────────────────────────
        # Large content is tagged for observability.  When content classification
        # is enabled (Phase 4), oversized content is split into sections in Gate 3.
        max_len = self._config.max_content_length
        if len(content) > max_len:
            logger.info(
                "Content size: %d chars exceeds %d (importance=%.1f) "
                "— %s",
                len(content), max_len, importance,
                "will split via section extraction"
                if self._config.content_classification_enabled
                else "proceeding as atomic (classification disabled)",
            )
            _emit_stage("size_flag", (time.perf_counter() - pipeline_start) * 1000, {
                "content_length": len(content),
                "max_content_length": max_len,
                "importance": importance,
                "classification_enabled": self._config.content_classification_enabled,
            })
            if tags is None:
                tags = []
            tags = list(tags) + ["oversized_content"]

        # ── Gate 3: Content classification (Phase 4, optional) ───────────
        if self._config.content_classification_enabled and self._section_svc is not None:
            try:
                from ncms.domain.content_classifier import (
                    ContentClass,
                    classify_content,
                    extract_sections,
                )

                t0 = time.perf_counter()
                classification = classify_content(content, memory_type)
                if classification.content_class == ContentClass.NAVIGABLE:
                    sections = extract_sections(content, classification)
                    if len(sections) >= 2:
                        _emit_stage(
                            "content_classification",
                            (time.perf_counter() - t0) * 1000,
                            {
                                "content_class": classification.content_class.value,
                                "format_hint": classification.format_hint,
                                "section_count": len(sections),
                            },
                        )
                        return await self._section_svc.ingest_navigable(  # type: ignore[union-attr]
                            content=content,
                            classification=classification,
                            sections=sections,
                            memory_type=memory_type,
                            importance=importance,
                            tags=tags,
                            structured=structured,
                            source=source_agent,
                            agent_id=source_agent,
                        )
                _emit_stage(
                    "content_classification",
                    (time.perf_counter() - t0) * 1000,
                    {
                        "content_class": classification.content_class.value,
                        "format_hint": classification.format_hint,
                        "section_count": 0,
                        "result": "atomic_passthrough",
                    },
                )
            except Exception:
                logger.warning(
                    "Content classification failed, proceeding as atomic",
                    exc_info=True,
                )
                _emit_stage(
                    "content_classification_error",
                    (time.perf_counter() - pipeline_start) * 1000,
                )

        # ── Admission scoring (Phase 1, optional) ────────────────────────
        admission_route: str | None = None
        admission_features: object | None = None
        if self._admission is not None and self._config.admission_enabled:
            result = await self._gate_admission(
                content=content, domains=domains, tags=tags,
                source_agent=source_agent, project=project,
                memory_type=memory_type, importance=importance,
                structured=structured,
                _emit_stage=_emit_stage, pipeline_start=pipeline_start,
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
        all_entities, linked_entity_ids = await self._run_inline_indexing(
            memory=memory, content=content, domains=domains,
            entities_manual=entities, _emit_stage=_emit_stage,
        )

        # Contradiction detection — fire-and-forget async task (deferred).
        # Memory is already stored and indexed; contradiction is metadata
        # enrichment, not a gate.  This avoids blocking ingestion for the
        # 500-2000ms LLM round-trip.
        if self._config.contradiction_detection_enabled:
            asyncio.create_task(
                self._deferred_contradiction_check(
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
                await self._create_memory_nodes(
                    memory=memory,
                    content=content,
                    all_entities=all_entities,
                    linked_entity_ids=linked_entity_ids,
                    admission_features=admission_features,
                    _emit_stage=_emit_stage,
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

    # ── Inline Indexing (BM25 + SPLADE + GLiNER + Entity Linking) ─────

    async def _run_inline_indexing(
        self,
        memory: Memory,
        content: str,
        domains: list[str] | None,
        entities_manual: list[dict] | None,
        _emit_stage: Callable,
    ) -> tuple[list[dict], list[str]]:
        """Run BM25, SPLADE, GLiNER in parallel then link entities + co-occurrence edges.

        Returns (all_entities, linked_entity_ids).
        """
        from ncms.infrastructure.extraction.gliner_extractor import extract_entities_gliner

        async def _do_bm25() -> float:
            t = time.perf_counter()
            await asyncio.to_thread(self._index.index_memory, memory)
            return (time.perf_counter() - t) * 1000

        async def _do_splade() -> float:
            if self._splade is None:
                return 0.0
            t = time.perf_counter()
            try:
                await asyncio.to_thread(self._splade.index_memory, memory)
            except Exception:
                logger.warning(
                    "SPLADE indexing failed for %s, continuing", memory.id, exc_info=True,
                )
            return (time.perf_counter() - t) * 1000

        async def _do_gliner() -> tuple[list[dict[str, str]], float]:
            t = time.perf_counter()
            cached = await self._get_cached_labels(domains or [])
            gliner_labels = resolve_labels(domains or [], cached_labels=cached)
            result = await asyncio.to_thread(
                extract_entities_gliner, content,
                model_name=self._config.gliner_model,
                threshold=self._config.gliner_threshold,
                labels=gliner_labels,
                cache_dir=self._config.model_cache_dir,
            )
            return result, (time.perf_counter() - t) * 1000

        logger.info("[store] Starting parallel indexing: BM25 + SPLADE + GLiNER")
        bm25_ms, splade_ms, (auto_entities, extract_ms) = await asyncio.gather(
            _do_bm25(), _do_splade(), _do_gliner(),
        )
        logger.info(
            "[store] Parallel indexing complete: BM25=%.0fms SPLADE=%.0fms GLiNER=%.0fms",
            bm25_ms, splade_ms, extract_ms,
        )

        _emit_stage("bm25_index", bm25_ms, memory_id=memory.id)
        if self._splade is not None:
            _emit_stage("splade_index", splade_ms, memory_id=memory.id)

        # Merge manual + auto-extracted entities (dedup by name)
        manual = list(entities_manual or [])
        manual_names = {e["name"].lower() for e in manual}
        all_entities = manual + [e for e in auto_entities if e["name"].lower() not in manual_names]
        _emit_stage("entity_extraction", extract_ms, {
            "extractor": "gliner", "auto_count": len(auto_entities),
            "manual_count": len(manual), "total_count": len(all_entities),
            "entity_names": [e["name"] for e in all_entities[:10]],
        }, memory_id=memory.id)

        # Link entities to memory in graph + store
        t0 = time.perf_counter()
        linked_entity_ids: list[str] = []
        for e_data in all_entities:
            entity = await self.add_entity(
                name=e_data["name"],
                entity_type=e_data.get("type", "concept"),
                attributes=e_data.get("attributes", {}),
            )
            linked_entity_ids.append(entity.id)
            await self._store.link_memory_entity(memory.id, entity.id)
            self._graph.link_memory_entity(memory.id, entity.id)
        _emit_stage("graph_linking", (time.perf_counter() - t0) * 1000, {
            "entities_linked": len(all_entities),
        }, memory_id=memory.id)

        # Co-occurrence edges: connect entities in same document for graph traversal
        if len(linked_entity_ids) > 1:
            self._build_cooccurrence_edges(
                memory.id, linked_entity_ids, _emit_stage,
            )

        return all_entities, linked_entity_ids

    def _build_cooccurrence_edges(
        self,
        memory_id: str,
        linked_entity_ids: list[str],
        _emit_stage: Callable,
    ) -> None:
        """Build co-occurrence edges between entities in the same memory."""
        t0 = time.perf_counter()
        cooc_ids = linked_entity_ids[: self._config.cooccurrence_max_entities]
        edges_new = 0
        edges_incremented = 0
        for i, a in enumerate(cooc_ids):
            for b in cooc_ids[i + 1:]:
                existing_count = self._graph.get_edge_cooccurrence(a, b)
                if existing_count > 0:
                    self._graph.increment_edge_cooccurrence(a, b)
                    self._graph.increment_edge_cooccurrence(b, a)
                    edges_incremented += 1
                else:
                    rel_ab = Relationship(
                        source_entity_id=a, target_entity_id=b,
                        type="co_occurs", source_memory_id=memory_id,
                    )
                    rel_ba = Relationship(
                        source_entity_id=b, target_entity_id=a,
                        type="co_occurs", source_memory_id=memory_id,
                    )
                    self._graph.add_relationship(rel_ab)
                    self._graph.add_relationship(rel_ba)
                    edges_new += 1
        _emit_stage("cooccurrence_edges", (time.perf_counter() - t0) * 1000, {
            "edges_new": edges_new, "edges_incremented": edges_incremented,
            "entities_used": len(cooc_ids),
            "entities_capped": len(linked_entity_ids) > self._config.cooccurrence_max_entities,
        }, memory_id=memory_id)

    # ── Admission Gate ─────────────────────────────────────────────────

    async def _gate_admission(
        self,
        content: str,
        domains: list[str] | None,
        tags: list[str] | None,
        source_agent: str | None,
        project: str | None,
        memory_type: str,
        importance: float,
        structured: dict | None,
        _emit_stage: Callable,
        pipeline_start: float,
    ) -> Memory | tuple[str | None, object | None, dict | None]:
        """Run admission scoring. Returns Memory for early exit (discard/ephemeral)
        or (route, features, structured) tuple to continue the persist path."""
        from dataclasses import asdict as _asdict

        from ncms.domain.models import EphemeralEntry
        from ncms.domain.scoring import route_memory, score_admission

        _skip_routing = importance >= 8.0

        t0 = time.perf_counter()
        try:
            features = await self._admission.compute_features(
                content, domains=domains, source_agent=source_agent,
            )
            score = score_admission(features)
            route = route_memory(features, score)

            feature_dict = _asdict(features)
            _emit_stage("admission", (time.perf_counter() - t0) * 1000, {
                "score": round(score, 3), "route": route,
                "features": {k: round(v, 3) for k, v in feature_dict.items()},
            })
            self._event_log.admission_scored(
                memory_id=None, score=score, route=route,
                features=feature_dict, agent_id=source_agent,
            )

            if _skip_routing:
                route = None
                logger.debug(
                    "Admission: features computed (state_change=%.2f) but "
                    "routing skipped for high-importance content (%.1f)",
                    features.state_change_signal, importance,
                )
            elif route == "discard":
                logger.info("Admission: discarding content (score=%.3f)", score)
                _emit_stage("complete", (time.perf_counter() - pipeline_start) * 1000, {
                    "result": "discarded", "admission_score": round(score, 3),
                })
                return Memory(
                    content=content, type=cast(Any, memory_type),
                    domains=domains or [], tags=tags or [],
                    source_agent=source_agent, project=project,
                    structured={"admission": {"score": score, "route": "discard"}},
                )
            elif route == "ephemeral_cache":
                from datetime import UTC, datetime, timedelta

                ttl = self._config.admission_ephemeral_ttl_seconds
                now = datetime.now(UTC)
                entry = EphemeralEntry(
                    content=content, source_agent=source_agent,
                    domains=domains or [], admission_score=score,
                    ttl_seconds=ttl, created_at=now,
                    expires_at=now + timedelta(seconds=ttl),
                )
                await self._store.save_ephemeral(entry)
                logger.info("Admission: ephemeral cache (score=%.3f, ttl=%ds)", score, ttl)
                _emit_stage("complete", (time.perf_counter() - pipeline_start) * 1000, {
                    "result": "ephemeral", "admission_score": round(score, 3),
                    "ephemeral_id": entry.id,
                })
                return Memory(
                    content=content, type=cast(Any, memory_type),
                    domains=domains or [], tags=tags or [],
                    source_agent=source_agent, project=project,
                    structured={"admission": {
                        "score": score, "route": "ephemeral_cache",
                        "ephemeral_id": entry.id,
                    }},
                )

            # Persist path: attach features as structured metadata
            if structured is None:
                structured = {}
            structured["admission"] = {
                "score": round(score, 3), "route": route,
                **{k: round(v, 3) for k, v in feature_dict.items()},
            }
            return route, features, structured

        except Exception:
            logger.warning(
                "Admission scoring failed, proceeding without admission",
                exc_info=True,
            )
            _emit_stage("admission_error", (time.perf_counter() - t0) * 1000)
            return None, None, structured

    # ── Node Creation (L1/L2) + Reconciliation + Episodes ──────────────

    async def _create_memory_nodes(
        self,
        memory: Memory,
        content: str,
        all_entities: list[dict],
        linked_entity_ids: list[str],
        admission_features: object | None,
        _emit_stage: Callable,
    ) -> None:
        """Create HTMG nodes for a persisted memory.

        L1 ATOMIC node is always created. L2 ENTITY_STATE node is additionally
        created if state change or state declaration is detected. Then reconcile
        against existing states and assign to an episode.
        """
        from ncms.domain.models import MemoryNode, NodeType

        # L1: ALWAYS create atomic node for persisted content
        l1_node = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ATOMIC,
            importance=memory.importance,
        )
        await self._store.save_memory_node(l1_node)
        _emit_stage("memory_node", 0.0, {
            "node_id": l1_node.id, "node_type": "atomic", "layer": "L1",
        }, memory_id=memory.id)

        # L2: Detect state change or state declaration
        l2_node = await self._detect_and_create_l2_node(
            memory, content, all_entities, l1_node,
            admission_features, _emit_stage,
        )

        # Phase 2A: Reconcile entity state against existing states
        if (
            l2_node is not None
            and self._reconciliation is not None
            and self._config.reconciliation_enabled
            and l2_node.metadata.get("entity_id")
        ):
            await self._reconcile_entity_state(l2_node, memory.id, _emit_stage)

        # Phase 3: Episode formation (links to L1 atomic node)
        if self._episode is not None and self._config.episodes_enabled:
            await self._assign_episode(
                l1_node, memory, content, linked_entity_ids, _emit_stage,
            )

    async def _detect_and_create_l2_node(
        self,
        memory: Memory,
        content: str,
        all_entities: list[dict],
        l1_node: object,
        admission_features: object | None,
        _emit_stage: Callable,
    ) -> object | None:
        """Detect entity state change and create L2 ENTITY_STATE node if found."""
        from ncms.domain.models import EdgeType, GraphEdge, MemoryNode, NodeType

        _has_state_change = (
            admission_features is not None
            and hasattr(admission_features, "state_change_signal")
            and admission_features.state_change_signal >= 0.35
        )
        _has_state_declaration = bool(
            re.search(
                r"^[a-zA-Z0-9_\-]+\s*:\s*[a-zA-Z0-9_\-]+\s*=\s*.+$",
                content, re.MULTILINE,
            )
            or re.search(r"(?:^|\n)##?\s*[Ss]tatus\s*[\n:]\s*\w+", content)
            or re.search(
                r"^\s*status\s*:\s*\w+", content, re.MULTILINE | re.IGNORECASE,
            )
        )

        if not (_has_state_change or _has_state_declaration):
            return None

        node_metadata = self._extract_entity_state_meta(content, all_entities)

        # Validate detected entity exists in GLiNER extraction set
        _entity_names_lower = {e["name"].lower() for e in all_entities}
        _detected_entity = node_metadata.get("entity_id", "")
        if _detected_entity.lower() not in _entity_names_lower:
            return None

        if not node_metadata:
            return None

        l2_node = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ENTITY_STATE,
            importance=memory.importance,
            metadata=node_metadata,
        )
        await self._store.save_memory_node(l2_node)

        # DERIVED_FROM edge: L2 → L1
        await self._store.save_graph_edge(GraphEdge(
            source_id=l2_node.id,
            target_id=l1_node.id,
            edge_type=EdgeType.DERIVED_FROM,
            metadata={"layer": "L2_from_L1"},
        ))
        _emit_stage("memory_node", 0.0, {
            "node_id": l2_node.id, "node_type": "entity_state", "layer": "L2",
            "derived_from": l1_node.id,
            "has_entity_state": bool(node_metadata.get("entity_id")),
        }, memory_id=memory.id)

        return l2_node

    async def _reconcile_entity_state(
        self,
        l2_node: object,
        memory_id: str,
        _emit_stage: Callable,
    ) -> None:
        """Reconcile an L2 entity_state node against existing states."""
        t0 = time.perf_counter()
        try:
            results = await self._reconciliation.reconcile(l2_node)  # type: ignore[attr-defined]
            _emit_stage("reconciliation", (time.perf_counter() - t0) * 1000, {
                "node_id": l2_node.id,
                "results_count": len(results),
                "relations": [
                    {"relation": r.relation, "existing": r.existing_node_id}
                    for r in results
                ],
            }, memory_id=memory_id)
        except Exception:
            logger.warning(
                "Reconciliation failed for node %s, continuing",
                l2_node.id, exc_info=True,
            )
            _emit_stage(
                "reconciliation_error", (time.perf_counter() - t0) * 1000,
                memory_id=memory_id,
            )

    async def _assign_episode(
        self,
        l1_node: object,
        memory: Memory,
        content: str,
        linked_entity_ids: list[str],
        _emit_stage: Callable,
    ) -> None:
        """Assign a memory's L1 node to an episode."""
        t0 = time.perf_counter()
        try:
            episode_node = await self._episode.assign_or_create(  # type: ignore[attr-defined]
                fragment_node=l1_node,
                fragment_memory=memory,
                entity_ids=linked_entity_ids,
            )
            _emit_stage("episode_formation", (time.perf_counter() - t0) * 1000, {
                "node_id": l1_node.id,
                "episode_id": episode_node.id if episode_node else None,
                "action": "created" if episode_node else "none",
            }, memory_id=memory.id)

            if episode_node is not None:
                await self._episode.check_resolution_closure(  # type: ignore[attr-defined]
                    content, episode_node,
                )
        except Exception:
            logger.warning(
                "Episode formation failed for node %s, continuing",
                l1_node.id, exc_info=True,
            )
            _emit_stage(
                "episode_formation_error", (time.perf_counter() - t0) * 1000,
                memory_id=memory.id,
            )

    # ── Deferred Contradiction Detection ────────────────────────────────

    async def _deferred_contradiction_check(
        self,
        memory: Memory,
        all_entities: list[dict],
        pipeline_id: str,
        source_agent: str | None = None,
    ) -> None:
        """Run contradiction detection as a post-ingest background task.

        Memory is already stored and indexed.  If contradictions are found,
        annotates the new memory and existing memories with metadata and
        emits a pipeline event.  Entirely non-fatal — errors are logged
        and swallowed.
        """
        t0 = time.perf_counter()
        contradiction_count = 0
        candidates_checked = 0
        try:
            from ncms.infrastructure.llm.contradiction_detector import (
                detect_contradictions,
            )

            # Find similar existing memories (new memory already indexed)
            candidates = self._index.search(
                memory.content, limit=self._config.contradiction_candidate_limit + 1,
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
                        "Deferred contradiction check: %d contradiction(s) for memory %s",
                        len(contradictions),
                        memory.id,
                    )
        except Exception:
            logger.warning(
                "Deferred contradiction detection failed for memory %s",
                memory.id,
                exc_info=True,
            )
        self._event_log.pipeline_stage(
            pipeline_id=pipeline_id, pipeline_type="store",
            stage="contradiction_deferred",
            duration_ms=(time.perf_counter() - t0) * 1000,
            data={
                "candidates_checked": candidates_checked,
                "contradictions_found": contradiction_count,
            },
            agent_id=source_agent, memory_id=memory.id,
        )

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

        # Phase 4: Intent classification (BM25 exemplar index → keyword fallback)
        intent_result: IntentResult | None = None

        # Phase 6: Explicit intent override bypasses classifier entirely
        if intent_override is not None:
            from ncms.domain.intent import INTENT_TARGETS

            try:
                qi = QueryIntent(intent_override)
            except ValueError:
                valid = [e.value for e in QueryIntent]
                raise ValueError(  # noqa: B904
                    f"Invalid intent '{intent_override}'. "
                    f"Valid intents: {valid}"
                )
            intent_result = IntentResult(
                intent=qi,
                confidence=1.0,
                target_node_types=INTENT_TARGETS.get(qi, ("atomic",)),
            )
            _emit_stage("intent_override", 0.0, {
                "intent": qi.value, "source": "user_override",
            })
        elif self._config.intent_classification_enabled:
            t0 = time.perf_counter()
            if self._intent_classifier is not None:
                intent_result = self._intent_classifier.classify(query)  # type: ignore[union-attr]
            else:
                intent_result = classify_intent(query)
            # Fall back to fact_lookup if confidence below threshold
            llm_fallback_used = False
            if intent_result.confidence < self._config.intent_confidence_threshold:
                # Optional LLM fallback for low-confidence classifications
                if self._config.intent_llm_fallback_enabled:
                    from ncms.infrastructure.llm.intent_classifier_llm import (
                        classify_intent_with_llm,
                    )

                    llm_result = await classify_intent_with_llm(
                        query,
                        model=self._config.llm_model,
                        api_base=self._config.llm_api_base,
                    )
                    if llm_result is not None:
                        intent_result = llm_result
                        llm_fallback_used = True
                    else:
                        # LLM failed — log the miss for exemplar tuning
                        _emit_stage("intent_llm_miss", 0, {
                            "query": query[:200],
                            "bm25_intent": intent_result.intent.value,
                            "bm25_confidence": round(intent_result.confidence, 3),
                        })

                # Still below threshold after LLM → default to fact_lookup
                if intent_result.confidence < self._config.intent_confidence_threshold:
                    _emit_stage("intent_miss", 0, {
                        "query": query[:200],
                        "best_intent": intent_result.intent.value,
                        "best_confidence": round(intent_result.confidence, 3),
                        "llm_attempted": llm_fallback_used,
                    })
                    intent_result = IntentResult(
                        intent=QueryIntent.FACT_LOOKUP,
                        confidence=1.0,
                        target_node_types=("atomic", "entity_state"),
                    )
            _emit_stage("intent_classification", (time.perf_counter() - t0) * 1000, {
                "intent": intent_result.intent.value,
                "confidence": round(intent_result.confidence, 3),
                "target_node_types": list(intent_result.target_node_types),
                "llm_fallback": llm_fallback_used,
            })

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

        # Tier 1: BM25 candidate retrieval via Tantivy
        # ── Parallel candidate retrieval: BM25 + SPLADE + entity extraction ──
        # These three operations are independent CPU/GPU-bound tasks.
        # Running them concurrently via asyncio.to_thread saves ~30-50% latency.
        import asyncio

        from ncms.infrastructure.extraction.gliner_extractor import extract_entities_gliner

        search_domains = [domain] if domain else []
        cached = await self._get_cached_labels(search_domains)
        labels = resolve_labels(search_domains, cached_labels=cached)

        t0 = time.perf_counter()

        async def _bm25_task() -> list[tuple[str, float]]:
            t = time.perf_counter()
            result = await asyncio.to_thread(
                self._index.search, query, self._config.tier1_candidates,
            )
            bm25_ms = (time.perf_counter() - t) * 1000
            logger.info("[search] BM25 done: %d results (%.0fms)", len(result), bm25_ms)
            return result

        async def _splade_task() -> list[tuple[str, float]]:
            if self._splade is None:
                return []
            try:
                t = time.perf_counter()
                result = await asyncio.to_thread(
                    self._splade.search, query, self._config.splade_top_k,
                )
                splade_ms = (time.perf_counter() - t) * 1000
                logger.info("[search] SPLADE done: %d results (%.0fms)", len(result), splade_ms)
                return result
            except Exception:
                logger.warning("SPLADE search failed, using BM25 only", exc_info=True)
                return []

        async def _entity_task() -> list[dict]:
            t = time.perf_counter()
            result = await asyncio.to_thread(
                extract_entities_gliner,
                query,
                model_name=self._config.gliner_model,
                threshold=self._config.gliner_threshold,
                labels=labels,
                cache_dir=self._config.model_cache_dir,
            )
            gliner_ms = (time.perf_counter() - t) * 1000
            logger.info("[search] GLiNER done: %d entities (%.0fms)", len(result), gliner_ms)
            return result

        logger.info("[search] Starting parallel retrieval: BM25 + SPLADE + GLiNER")
        bm25_results, splade_results, query_entity_names = await asyncio.gather(
            _bm25_task(), _splade_task(), _entity_task(),
        )
        parallel_ms = (time.perf_counter() - t0) * 1000
        logger.info("[search] Parallel retrieval complete (%.0fms total)", parallel_ms)

        # Emit stage events for observability
        bm25_data: dict[str, object] = {
            "candidate_count": len(bm25_results),
            "top_score": round(bm25_results[0][1], 3) if bm25_results else None,
        }
        _emit_stage("bm25", parallel_ms, bm25_data)
        if splade_results:
            _emit_stage("splade", parallel_ms, {
                "candidate_count": len(splade_results),
            })

        # Fuse BM25 + SPLADE via Reciprocal Rank Fusion
        if splade_results:
            t0 = time.perf_counter()
            fused_candidates = self._rrf_fuse(bm25_results, splade_results)
            _emit_stage(
                "rrf_fusion", (time.perf_counter() - t0) * 1000,
                {"fused_count": len(fused_candidates)},
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

        # ── Cross-encoder reranking (Phase 10) ────────────────────────
        # Rerank top RRF candidates using a cross-encoder model.
        # Phase 11: Only apply CE for intents where it helps (fact-finding,
        # pattern matching). Skip for temporal/state queries where CE hurts
        # CR and LRU by ignoring recency and temporal ordering.
        ce_intents = {
            QueryIntent.FACT_LOOKUP,
            QueryIntent.PATTERN_LOOKUP,
            QueryIntent.STRATEGIC_REFLECTION,
        }
        _use_ce = (
            self._reranker is not None
            and self._config.reranker_enabled
            and (intent_result is None or intent_result.intent in ce_intents)
        )
        ce_scores: dict[str, float] = {}
        if _use_ce:
            logger.info(
                "[search] Starting cross-encoder reranking (%d candidates)",
                len(fused_candidates),
            )
            t0 = time.perf_counter()
            rerank_ids = [mid for mid, _ in fused_candidates[
                :self._config.reranker_top_k
            ]]
            rerank_memories = await self._store.get_memories_batch(rerank_ids)
            rerank_pairs = [
                (mid, rerank_memories[mid].content)
                for mid in rerank_ids if mid in rerank_memories
            ]
            assert self._reranker is not None  # guarded by _use_ce
            reranked = await asyncio.to_thread(
                self._reranker.rerank, query, rerank_pairs,
                self._config.reranker_output_k,
            )
            ce_scores = {mid: score for mid, score in reranked}
            # Replace fused candidates with reranked order
            fused_candidates = reranked
            ce_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[search] Cross-encoder done: %d\u2192%d results (%.0fms)",
                len(rerank_pairs), len(reranked), ce_ms,
            )
            _emit_stage("cross_encoder_rerank", ce_ms, {
                "input_count": len(rerank_pairs),
                "output_count": len(reranked),
                "top_score": round(reranked[0][1], 4) if reranked else None,
            })

        # Build per-source score lookups
        bm25_scores: dict[str, float] = {mid: score for mid, score in bm25_results}
        splade_scores: dict[str, float] = {mid: score for mid, score in splade_results}

        # Resolve entity names to IDs
        # Use graph O(1) name index when available, fall back to SQLite
        context_entity_ids: list[str] = []
        for qe in query_entity_names:
            eid = self._graph.find_entity_by_name(qe["name"])
            if eid:
                context_entity_ids.append(eid)
            else:
                existing = await self._store.find_entity_by_name(qe["name"])
                if existing:
                    context_entity_ids.append(existing.id)
        _emit_stage("entity_extraction", parallel_ms, {
            "query_entities": [e["name"] for e in query_entity_names[:10]],
            "context_entity_count": len(context_entity_ids),
        })

        # Phase 9: Query expansion — inject PMI-learned terms into BM25
        if self._config.dream_query_expansion_enabled and context_entity_ids:
            try:
                expansion_terms = await self._get_query_expansion_terms(
                    context_entity_ids
                )
                if expansion_terms:
                    expanded_query = query + " " + " ".join(expansion_terms)
                    expanded_bm25 = self._index.search(
                        expanded_query, limit=self._config.tier1_candidates,
                    )
                    # Merge expanded results into bm25_scores (take max)
                    # AND inject novel candidates into fused_candidates so they
                    # enter the scoring loop (not just update scores for existing)
                    existing_fused = {mid for mid, _ in fused_candidates}
                    novel_from_expansion = 0
                    for mid, score in expanded_bm25:
                        if mid not in bm25_scores or score > bm25_scores[mid]:
                            bm25_scores[mid] = score
                        if mid not in existing_fused:
                            fused_candidates.append((mid, score))
                            existing_fused.add(mid)
                            novel_from_expansion += 1
                    _emit_stage("query_expansion", 0, {
                        "terms": expansion_terms,
                        "expanded_candidates": len(expanded_bm25),
                        "novel_candidates": novel_from_expansion,
                    })
            except Exception:
                logger.debug("Query expansion failed", exc_info=True)

        # ── Tier 1.5: Graph-expanded candidate discovery ────────────────
        # Collect entity IDs from fused hits, then discover related memories
        # via shared graph entities that search missed lexically.
        fused_ids = {mid for mid, _ in fused_candidates}
        all_candidates: list[tuple[str, float]] = list(fused_candidates)

        # Graph expansion (always on — Tier 1.5)
        if True:
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

        # Phase 4: Batch-load memory nodes for intent scoring + reconciliation
        nodes_by_memory: dict[str, list] = {}
        if self._config.intent_classification_enabled or self._config.reconciliation_enabled:
            t0_nodes = time.perf_counter()
            candidate_memory_ids = [mid for mid, _ in all_candidates]
            nodes_by_memory = await self._store.get_memory_nodes_for_memories(
                candidate_memory_ids,
            )
            _emit_stage("node_preload", (time.perf_counter() - t0_nodes) * 1000, {
                "candidate_count": len(candidate_memory_ids),
                "nodes_loaded": sum(len(v) for v in nodes_by_memory.values()),
            })

        # Phase 4: Inject supplementary candidates based on intent
        if intent_result and intent_result.intent != QueryIntent.FACT_LOOKUP:
            t0_supp = time.perf_counter()
            supplement_ids = await self._intent_supplement(
                intent_result, context_entity_ids, fused_ids,
            )
            for sid in supplement_ids:
                if sid not in fused_ids:
                    all_candidates.append((sid, 0.0))
                    fused_ids.add(sid)
            # Preload nodes for supplement candidates too
            if supplement_ids:
                supp_nodes = await self._store.get_memory_nodes_for_memories(
                    list(supplement_ids),
                )
                nodes_by_memory.update(supp_nodes)
            _emit_stage("intent_supplement", (time.perf_counter() - t0_supp) * 1000, {
                "intent": intent_result.intent.value,
                "supplement_count": len(supplement_ids),
                "total_candidates": len(all_candidates),
            })

        # Phase 8: Load learned association strengths for spreading activation
        assoc_strengths: dict[tuple[str, str], float] | None = None
        if self._config.dream_cycle_enabled:
            try:
                assoc_strengths = await self._store.get_association_strengths()
                if not assoc_strengths:
                    assoc_strengths = None  # Fall back to default overlap model
            except Exception:
                logger.debug("Failed to load association strengths", exc_info=True)

        # Compute IDF weights for entity-based scoring (Fix #3)
        # IDF = log(N / df) where N = total memories, df = memories containing entity
        entity_idf: dict[str, float] | None = None
        if context_entity_ids:  # Graph expansion always on
            try:
                doc_freq = self._graph.get_entity_document_frequency()
                total_docs = max(self._graph.total_memory_count(), 1)
                entity_idf = {}
                for eid, df in doc_freq.items():
                    if df > 0:
                        entity_idf[eid] = math.log(total_docs / df)
                    else:
                        entity_idf[eid] = 0.0
            except Exception:
                logger.debug("Failed to compute entity IDF", exc_info=True)

        # Personalized PageRank — compute ONCE per query (not per candidate)
        # Skip entirely when graph weight is 0 (saves ~5ms per query)
        ppr_scores: dict[str, float] = {}
        if (
            context_entity_ids
            and self._config.scoring_weight_graph > 0
        ):
            try:
                # Fix #3 (audit): seed PPR with uniform weights.
                # IDF is already applied in ppr_graph_score(); using IDF
                # here too double-dips and over-weights rare entities.
                seed = {eid: 1.0 for eid in context_entity_ids}
                ppr_scores = self._graph.personalized_pagerank(seed)
                # Normalize PPR to [0, 1] range so graph signal competes with BM25.
                # Raw PPR is a probability distribution (sums to 1.0 over ~3K entities),
                # producing per-entity values ~0.0003 — 1000x smaller than BM25 scores.
                max_ppr = max(ppr_scores.values()) if ppr_scores else 0.0
                if max_ppr > 0:
                    ppr_scores = {k: v / max_ppr for k, v in ppr_scores.items()}
            except Exception:
                logger.debug("PPR computation failed, falling back to BFS", exc_info=True)

        # BFS fallback closures (only used when PPR disabled or fails)
        def _neighbor_fn(eid: str) -> list[tuple[str, float]]:
            return self._graph.get_neighbors_with_weights(eid)

        def _degree_fn(eid: str) -> int:
            return self._graph.get_entity_degree(eid)

        # Phase 9: Resolve signal weights (per-intent or global)
        w_bm25 = self._config.scoring_weight_bm25
        w_actr = self._config.scoring_weight_actr
        w_splade = self._config.scoring_weight_splade
        w_graph = self._config.scoring_weight_graph
        w_recency = self._config.scoring_weight_recency

        if self._config.intent_routing_enabled and intent_result:
            try:
                routed = self._get_intent_weights(intent_result.intent)
                w_bm25, w_splade, w_graph, w_recency = routed
            except Exception:
                logger.debug("Intent weight routing failed, using defaults", exc_info=True)

        # ── Batch preload: memories + access times ─────────────────────
        # Single SQL query each instead of N+1 per-candidate round-trips.
        # For 100 candidates this eliminates ~200 sequential DB calls.
        t0 = time.perf_counter()
        candidate_ids = [mid for mid, _ in all_candidates]
        memories_batch = await self._store.get_memories_batch(candidate_ids)
        # Skip access times load when ACT-R is disabled (saves ~50ms per query)
        if w_actr > 0:
            access_times_batch = await self._store.get_access_times_batch(candidate_ids)
        else:
            access_times_batch = {}

        # ── Pass 1: Compute raw signals for all candidates ─────────────
        # Two-pass scoring: first collect raw signals, then normalize
        # to [0, 1] so weights actually control relative importance.
        raw_candidates: list[dict] = []
        candidates_scored = 0

        for memory_id, _fused_score in all_candidates:
            memory = memories_batch.get(memory_id)
            if not memory:
                continue

            # Domain filter (exact match or prefix match)
            if domain and domain not in memory.domains and not any(
                d.startswith(domain) for d in memory.domains
            ):
                continue

            # Tier 2: ACT-R activation scoring (from batch-loaded access times)
            access_ages = access_times_batch.get(memory_id, [])
            bl = base_level_activation(access_ages, decay=self._config.actr_decay)

            # Spreading activation: two separate signals
            # 1. ACT-R spread: Jaccard entity overlap (for total_activation)
            # 2. Graph spread: PPR or BFS traversal (for w_graph)
            memory_entities = self._graph.get_entity_ids_for_memory(memory_id)

            # Only compute Jaccard spread if ACT-R is enabled (saves CPU)
            spread = 0.0
            if w_actr > 0:
                spread = spreading_activation(
                    memory_entity_ids=memory_entities,
                    context_entity_ids=context_entity_ids,
                    association_strengths=assoc_strengths,
                    source_activation=self._config.actr_max_spread,
                )

            # Phase 9: PPR graph score (or BFS fallback)
            if ppr_scores:
                graph_spread = ppr_graph_score(
                    memory_entity_ids=memory_entities,
                    ppr_scores=ppr_scores,
                    entity_idf=entity_idf,
                )
            else:
                graph_spread = graph_spreading_activation(
                    memory_entity_ids=memory_entities,
                    context_entity_ids=context_entity_ids,
                    neighbor_fn=_neighbor_fn,
                    entity_idf=entity_idf,
                    hop_decay=self._config.graph_hop_decay,
                    max_hops=self._config.graph_spreading_max_hops,
                    source_activation=self._config.actr_max_spread,
                    degree_fn=_degree_fn,
                )

            noise = activation_noise(sigma=self._config.actr_noise)

            # Load memory nodes (batch-preloaded or per-candidate fallback)
            nodes = nodes_by_memory.get(memory_id, [])
            candidate_node_types = [mn.node_type.value for mn in nodes]

            # Phase 2C: reconciliation penalties for superseded / conflicted states
            mem_is_superseded = False
            mem_has_conflicts = False
            mem_superseded_by: str | None = None
            penalty = 0.0
            if self._config.reconciliation_enabled and nodes:
                try:
                    from ncms.domain.models import EdgeType

                    for mn in nodes:
                        if not mn.is_current:
                            mem_is_superseded = True
                            mem_superseded_by = mn.metadata.get("superseded_by")
                        conflict_edges = await self._store.get_graph_edges(
                            mn.id, EdgeType.CONFLICTS_WITH,
                        )
                        if conflict_edges:
                            mem_has_conflicts = True
                    penalty = (
                        supersession_penalty(
                            mem_is_superseded,
                            self._config.reconciliation_supersession_penalty,
                        )
                        + conflict_annotation_penalty(
                            mem_has_conflicts,
                            self._config.reconciliation_conflict_penalty,
                        )
                    )
                except Exception:
                    logger.debug(
                        "Reconciliation penalty lookup failed for %s",
                        memory_id, exc_info=True,
                    )

            # Phase 4: Hierarchy match bonus
            h_bonus = 0.0
            if intent_result and candidate_node_types:
                h_bonus = hierarchy_match_bonus(
                    candidate_node_types,
                    intent_result.target_node_types,
                    bonus=self._config.intent_hierarchy_bonus,
                )

            # Penalty is applied in combined score (Pass 2), not inside ACT-R,
            # to avoid double-counting when w_actr > 0.
            act = total_activation(bl, spread, noise, mismatch_penalty=0.0)

            # Raw per-source scores (NOT yet normalized)
            bm25_score = bm25_scores.get(memory_id, 0.0)
            splade_score_val = splade_scores.get(memory_id, 0.0)

            # Recency scoring: exponential decay based on memory age
            rec_score = 0.0
            if w_recency > 0 and memory.created_at:
                from datetime import UTC, datetime
                now = datetime.now(UTC)
                age_seconds = max(0.0, (now - memory.created_at).total_seconds())
                rec_score = recency_score(
                    age_seconds,
                    half_life_days=self._config.recency_half_life_days,
                )

            # Phase 4 temporal: compute temporal proximity score
            temporal_raw = 0.0
            if temporal_ref is not None:
                # Prefer observed_at (bitemporal) over created_at
                event_time = memory.created_at
                if nodes:
                    for mn in nodes:
                        if mn.observed_at is not None:
                            event_time = mn.observed_at
                            break
                if event_time is not None:
                    temporal_raw = compute_temporal_proximity(
                        event_time, temporal_ref,
                    )

            candidates_scored += 1
            raw_candidates.append({
                "memory": memory,
                "memory_id": memory_id,
                "bm25_raw": bm25_score,
                "splade_raw": splade_score_val,
                "graph_raw": graph_spread,
                "temporal_raw": temporal_raw,
                "act": act,
                "bl": bl,
                "spread": spread,
                "noise": noise,
                "penalty": penalty,
                "h_bonus": h_bonus,
                "rec_score": rec_score,
                "is_superseded": mem_is_superseded,
                "has_conflicts": mem_has_conflicts,
                "superseded_by": mem_superseded_by,
                "node_types": candidate_node_types,
            })

        # ── Pass 2: Normalize signals and compute combined scores ─────
        # Per-query min-max normalization puts all signals in [0, 1]
        # so configured weights actually determine relative importance.
        # Without this, SPLADE (5-200) dominates BM25 (1-15) despite
        # lower weight, and graph signal is in yet another range.
        if raw_candidates:
            max_bm25 = max(c["bm25_raw"] for c in raw_candidates) or 1.0
            max_splade = max(c["splade_raw"] for c in raw_candidates) or 1.0
            max_graph = max(c["graph_raw"] for c in raw_candidates) or 1.0
            max_temporal = max(c["temporal_raw"] for c in raw_candidates) or 1.0
        else:
            max_bm25 = max_splade = max_graph = max_temporal = 1.0

        scored: list[ScoredMemory] = []
        filtered_below_threshold = 0
        top_activation = 0.0
        w_hierarchy = self._config.scoring_weight_hierarchy
        w_temporal = (
            self._config.scoring_weight_temporal
            if temporal_ref is not None else 0.0
        )
        actr_enabled = w_actr > 0
        w_ce = self._config.scoring_weight_ce if ce_scores else 0.0

        # CE score normalization (min-max)
        if ce_scores:
            ce_vals = [ce_scores.get(c["memory_id"], 0.0) for c in raw_candidates]
            max_ce = max(ce_vals) if ce_vals else 1.0
            min_ce = min(ce_vals) if ce_vals else 0.0
            ce_range = max_ce - min_ce if max_ce > min_ce else 1.0
        else:
            min_ce = 0.0
            ce_range = 1.0

        for c in raw_candidates:
            # Normalize each signal to [0, 1]
            bm25_norm = c["bm25_raw"] / max_bm25
            splade_norm = c["splade_raw"] / max_splade
            graph_norm = c["graph_raw"] / max_graph
            temporal_norm = c["temporal_raw"] / max_temporal

            # Combined score with normalized signals
            # When cross-encoder is active, CE dominates with BM25/SPLADE as tiebreakers.
            # Penalty applied ONLY here (not also inside ACT-R) to avoid
            # double-counting when w_actr > 0.
            # Temporal score is additive in both paths when a temporal
            # reference was detected.
            temporal_contrib = temporal_norm * w_temporal
            if ce_scores:
                ce_raw = ce_scores.get(c["memory_id"], min_ce)
                ce_norm = (ce_raw - min_ce) / ce_range
                combined = (
                    ce_norm * w_ce
                    + bm25_norm * (1.0 - w_ce) * 0.67  # BM25 tiebreaker
                    + splade_norm * (1.0 - w_ce) * 0.33  # SPLADE tiebreaker
                    + temporal_contrib
                    - c["penalty"]
                )
            else:
                combined = (
                    bm25_norm * w_bm25
                    + c["act"] * w_actr
                    + splade_norm * w_splade
                    + graph_norm * w_graph
                    + c["h_bonus"] * w_hierarchy
                    + c["rec_score"] * w_recency
                    + temporal_contrib
                    - c["penalty"]
                )

            if combined > top_activation:
                top_activation = combined

            # Retrieval probability filter:
            # When ACT-R is disabled (w_actr=0), bypass the ret_prob filter.
            # It uses ACT-R activation (which is meaningless at w_actr=0)
            # and incorrectly kills graph-expanded candidates that have
            # no access history but valid graph signal.
            ret_prob = 1.0
            if actr_enabled:
                ret_prob = retrieval_probability(
                    c["act"],
                    threshold=self._config.actr_threshold,
                    tau=self._config.actr_temperature,
                )
                if ret_prob < 0.05:
                    filtered_below_threshold += 1
                    continue

            scored.append(
                ScoredMemory(
                    memory=c["memory"],
                    bm25_score=c["bm25_raw"],
                    splade_score=c["splade_raw"],
                    base_level=c["bl"],
                    spreading=c["graph_raw"],
                    total_activation=combined,
                    retrieval_prob=ret_prob,
                    is_superseded=c["is_superseded"],
                    has_conflicts=c["has_conflicts"],
                    superseded_by=c["superseded_by"],
                    node_types=c["node_types"],
                    intent=intent_result.intent.value if intent_result else None,
                    hierarchy_bonus=c["h_bonus"],
                    temporal_score=temporal_contrib,
                )
            )

        actr_data: dict[str, object] = {
            "candidates_scored": candidates_scored,
            "passed_threshold": len(scored),
            "filtered_below_threshold": filtered_below_threshold,
            "top_activation": round(top_activation, 3),
            "normalization": {
                "max_bm25": round(max_bm25, 3),
                "max_splade": round(max_splade, 3),
                "max_graph": round(max_graph, 3),
                "max_temporal": round(max_temporal, 3),
            },
        }
        if self._config.pipeline_debug and scored:
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

        # Phase 6: Emit retrieval debug diagnostics when pipeline_debug is on
        if self._config.pipeline_debug and scored:
            intent_label = (
                intent_result.intent.value if intent_result else "unknown"
            )
            debug_candidates = [
                {
                    "id": s.memory.id,
                    "type": s.memory.type,
                    "content": s.memory.content[:120],
                    "bm25": round(s.bm25_score, 4),
                    "splade": round(s.splade_score, 4),
                    "graph": round(s.spreading, 4),
                    "actr": round(s.total_activation, 4),
                    "hierarchy": round(s.hierarchy_bonus, 4),
                    "superseded": s.is_superseded,
                    "conflicts": s.has_conflicts,
                    "node_types": s.node_types,
                }
                for s in sorted(
                    scored, key=lambda x: x.total_activation, reverse=True,
                )[:20]
            ]
            self._event_log.retrieval_debug(
                query=query,
                intent=intent_label,
                candidates=debug_candidates,
                scores={
                    "max_bm25": round(max_bm25, 3),
                    "max_splade": round(max_splade, 3),
                    "max_graph": round(max_graph, 3),
                    "max_temporal": round(max_temporal, 3),
                },
                agent_id=agent_id,
            )

        # Sort by combined score (descending) — Tier 2 ranking
        scored.sort(key=lambda s: s.total_activation, reverse=True)

        results = scored[:limit]

        # Log access ONLY for returned results (not all scored candidates).
        # Logging all scored candidates inflates access counts and distorts
        # ACT-R base-level activation for future queries.
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
            "total_candidates_evaluated": candidates_scored,
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

    # ── Intent Supplementary Candidates ──────────────────────────────────

    async def _intent_supplement(
        self,
        intent: IntentResult,
        context_entity_ids: list[str],
        already_seen: set[str],
    ) -> set[str]:
        """Generate supplementary candidate memory IDs for specialised intents.

        Returns memory_ids not already in the candidate set.
        """
        supplement: set[str] = set()
        max_supp = self._config.intent_supplement_max

        if intent.intent == QueryIntent.CURRENT_STATE_LOOKUP:
            for eid in context_entity_ids:
                states = await self._store.get_entity_states_by_entity(eid)
                for s in states:
                    if s.is_current and s.memory_id not in already_seen:
                        supplement.add(s.memory_id)
                        if len(supplement) >= max_supp:
                            return supplement

        elif intent.intent == QueryIntent.CHANGE_DETECTION:
            for eid in context_entity_ids:
                states = await self._store.get_entity_states_by_entity(eid)
                for s in states:
                    if s.memory_id not in already_seen:
                        supplement.add(s.memory_id)
                        if len(supplement) >= max_supp:
                            return supplement

        elif intent.intent == QueryIntent.EVENT_RECONSTRUCTION:
            episodes = await self._store.get_open_episodes()
            for ep in episodes[:5]:  # Cap episode lookups
                members = await self._store.get_episode_members(ep.id)
                for m in members:
                    if m.memory_id not in already_seen:
                        supplement.add(m.memory_id)
                        if len(supplement) >= max_supp:
                            return supplement

        elif intent.intent == QueryIntent.HISTORICAL_LOOKUP:
            from datetime import UTC, datetime, timedelta

            cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
            changes = await self._store.get_state_changes_since(cutoff)
            for c in changes:
                if c.memory_id not in already_seen:
                    supplement.add(c.memory_id)
                    if len(supplement) >= max_supp:
                        return supplement

        # pattern_lookup and strategic_reflection: no supplement until Phase 5

        return supplement

    # ── Phase 9: Per-Intent Weight Routing ────────────────────────────────

    def _get_intent_weights(self, intent: QueryIntent) -> tuple[float, float, float, float]:
        """Resolve (w_bm25, w_splade, w_graph, w_recency) for the classified intent.

        Returns a 4-tuple of weights parsed from the config string for this intent.
        Falls back to global defaults on parse error.
        """
        intent_key = intent.value  # e.g. "fact_lookup"
        config_attr = f"intent_weights_{intent_key}"
        raw = getattr(self._config, config_attr, None)
        if not raw:
            return (
                self._config.scoring_weight_bm25,
                self._config.scoring_weight_splade,
                self._config.scoring_weight_graph,
                self._config.scoring_weight_recency,
            )
        try:
            parts = [float(x.strip()) for x in raw.split(",")]
            if len(parts) != 4:
                raise ValueError(f"Expected 4 weights, got {len(parts)}")
            return (parts[0], parts[1], parts[2], parts[3])
        except (ValueError, TypeError):
            logger.warning("Invalid intent weights for %s: %r", intent_key, raw)
            return (
                self._config.scoring_weight_bm25,
                self._config.scoring_weight_splade,
                self._config.scoring_weight_graph,
                self._config.scoring_weight_recency,
            )

    # ── Phase 9: Query Expansion ──────────────────────────────────────────

    _query_expansion_dict: dict[str, list[str]] | None = None

    def invalidate_query_expansion_cache(self) -> None:
        """Clear cached expansion dict so next search reloads from DB.

        Call after dream cycle writes a new expansion dict.
        """
        self._query_expansion_dict = None

    async def _get_query_expansion_terms(
        self, context_entity_ids: list[str],
    ) -> list[str]:
        """Look up PMI-learned expansion terms for the query's entities.

        Loads the expansion dict from consolidation_state on first call
        (cached until invalidate_query_expansion_cache() is called).
        Returns a flat list of expansion term strings (entity names).
        """
        import json as _json

        # Lazy-load expansion dict (reloads after invalidation)
        if self._query_expansion_dict is None:
            raw = await self._store.get_consolidation_value("query_expansion_dict")
            if raw:
                try:
                    self._query_expansion_dict = _json.loads(raw)
                except Exception:
                    self._query_expansion_dict = {}
            else:
                self._query_expansion_dict = {}

        if not self._query_expansion_dict:
            return []

        # Round-robin allocation: each entity gets a fair share of expansion slots
        # (prevents first entity from hogging all max_terms slots)
        terms: list[str] = []
        seen: set[str] = set()
        max_terms = self._config.dream_expansion_max_terms
        n_entities = len(context_entity_ids) if context_entity_ids else 1
        per_entity = max(2, max_terms // n_entities)

        for eid in context_entity_ids:
            expansions = self._query_expansion_dict.get(eid, [])
            count = 0
            for term in expansions:
                if term not in seen and count < per_entity:
                    terms.append(term)
                    seen.add(term)
                    count += 1
            if len(terms) >= max_terms:
                break

        return terms[:max_terms]

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
            bonus_results = await self._recall_structured_state(
                context_entity_ids, intent, seen_memory_ids,
            )
        elif intent == QueryIntent.EVENT_RECONSTRUCTION:
            bonus_results = await self._recall_episode_bonus(
                scored, seen_memory_ids,
            )

        # 6. Merge: BM25 base first (preserves ranking), then bonus extras
        merged = base_results + bonus_results
        # Cap at limit but always keep all base results
        merged = merged[:max(limit, len(base_results))]

        # 7. Enrich all results with episode, entity state, and causal context
        enriched = await self._enrich_existing_results(merged)

        # 8. Expand document profiles into relevant sections
        enriched = await self._expand_document_sections(enriched, query)

        return enriched

    # ── Recall bonus helpers (layered on top of BM25 base) ──────────

    async def _recall_structured_state(
        self,
        entity_ids: list[str],
        intent: QueryIntent,
        seen_memory_ids: set[str],
    ) -> list[RecallResult]:
        """Fetch state-graph bonus results for state/historical/change intents.

        Returns memories from entity state graph that BM25 may have missed.
        These are prepended to BM25 results. Only includes memories NOT
        already in the BM25 result set (seen_memory_ids).
        """
        from ncms.domain.models import EntityStateMeta

        bonus: list[RecallResult] = []

        for eid in entity_ids[:5]:
            try:
                all_states = await self._store.get_entity_states_by_entity(eid)
                if intent == QueryIntent.CURRENT_STATE_LOOKUP:
                    state_nodes = [s for s in all_states if s.is_current]
                else:
                    # HISTORICAL_LOOKUP or CHANGE_DETECTION — full history
                    state_nodes = all_states
            except Exception:
                continue

            for sn in state_nodes:
                if sn.memory_id in seen_memory_ids:
                    continue
                memory = await self._store.get_memory(sn.memory_id)
                if not memory:
                    continue
                seen_memory_ids.add(sn.memory_id)
                meta = EntityStateMeta.from_node(sn)
                if meta is None:
                    continue
                scored = ScoredMemory(memory=memory, bm25_score=0.0)
                path = {
                    QueryIntent.CURRENT_STATE_LOOKUP: "state_lookup_bonus",
                    QueryIntent.HISTORICAL_LOOKUP: "state_history_bonus",
                    QueryIntent.CHANGE_DETECTION: "change_detection_bonus",
                }.get(intent, "state_bonus")
                bonus.append(RecallResult(
                    memory=scored,
                    context=RecallContext(
                        entity_states=[EntityStateSnapshot(
                            entity_id=eid,
                            entity_name=self._graph.get_entity_name(eid) or eid,
                            state_key=meta.state_key or "",
                            state_value=meta.state_value or "",
                            is_current=sn.is_current,
                            observed_at=sn.observed_at,
                        )],
                    ),
                    retrieval_path=path,
                ))

        return bonus

    async def _recall_episode_bonus(
        self,
        scored: list[ScoredMemory],
        seen_memory_ids: set[str],
    ) -> list[RecallResult]:
        """Expand episode abstracts from search results into member memories.

        For EVENT_RECONSTRUCTION: find episode summaries in the BM25 results,
        expand via DERIVED_FROM/SUMMARIZES edges to find member memories that
        BM25 may have missed. Returns only the bonus members not already in
        the search results.
        """
        bonus: list[RecallResult] = []
        abstracts = [s for s in scored if "abstract" in (s.node_types or [])]

        for abstract in abstracts[:5]:
            nodes = await self._store.get_memory_nodes_for_memory(abstract.memory.id)
            for node in nodes:
                try:
                    edges = await self._store.get_graph_edges(node.id)
                except Exception:
                    continue
                for edge in edges:
                    if edge.edge_type in ("derived_from", "summarizes"):
                        try:
                            target_node = await self._store.get_memory_node(
                                edge.target_id,
                            )
                        except Exception:
                            continue
                        if not target_node:
                            continue
                        mid = target_node.memory_id
                        if mid in seen_memory_ids:
                            continue
                        memory = await self._store.get_memory(mid)
                        if not memory:
                            continue
                        seen_memory_ids.add(mid)
                        sm = ScoredMemory(memory=memory, bm25_score=0.0)
                        bonus.append(RecallResult(
                            memory=sm,
                            retrieval_path="episode_expansion_bonus",
                        ))

        return bonus

    # ── Context enrichment ────────────────────────────────────────────

    async def _enrich_existing_results(
        self,
        results: list[RecallResult],
    ) -> list[RecallResult]:
        """Enrich RecallResult list with episode, entity state, and causal context.

        Operates in batch where possible to minimize DB round-trips.
        """
        if not results:
            return results

        # Batch preload memory nodes for all results
        memory_ids = [r.memory.memory.id for r in results]
        nodes_batch = await self._store.get_memory_nodes_for_memories(memory_ids)

        for result in results:
            mid = result.memory.memory.id
            nodes = nodes_batch.get(mid, [])
            entity_ids = self._graph.get_entity_ids_for_memory(mid)

            await self._enrich_entity_states(result, entity_ids)
            await self._enrich_episode_context(result, mid, nodes)
            await self._enrich_causal_chain(result, nodes)

        return results

    async def _enrich_entity_states(
        self,
        result: RecallResult,
        entity_ids: list[str],
    ) -> None:
        """Populate entity state snapshots on a RecallResult (cap at 10 entities)."""
        from ncms.domain.models import EntityStateMeta

        if result.context.entity_states or not entity_ids:
            return

        for eid in entity_ids[:10]:
            try:
                all_st = await self._store.get_entity_states_by_entity(eid)
                state_nodes = [s for s in all_st if s.is_current]
            except Exception:
                continue
            for sn in state_nodes:
                meta = EntityStateMeta.from_node(sn)
                if meta is None:
                    continue
                result.context.entity_states.append(
                    EntityStateSnapshot(
                        entity_id=eid,
                        entity_name=(self._graph.get_entity_name(eid) or eid),
                        state_key=meta.state_key or "",
                        state_value=meta.state_value or "",
                        is_current=sn.is_current,
                        observed_at=sn.observed_at,
                    )
                )

    async def _enrich_episode_context(
        self,
        result: RecallResult,
        memory_id: str,
        nodes: list,
    ) -> None:
        """Populate episode membership context on a RecallResult."""
        from ncms.domain.models import EpisodeMeta, NodeType

        if result.context.episode is not None:
            return

        for node in nodes:
            if not node.parent_id:
                continue
            try:
                ep_node = await self._store.get_memory_node(node.parent_id)
            except Exception:
                continue
            if not ep_node or ep_node.node_type != NodeType.EPISODE:
                continue
            ep_meta = EpisodeMeta.from_node(ep_node)
            if ep_meta is None:
                continue
            members = await self._store.get_episode_members(ep_node.id)
            summary_text = await self._find_episode_summary(ep_node.id)
            result.context.episode = EpisodeContext(
                episode_id=ep_node.id,
                episode_title=ep_meta.episode_title or "",
                status=ep_meta.status or "open",
                member_count=ep_meta.member_count or 0,
                topic_entities=ep_meta.topic_entities or [],
                sibling_ids=[
                    m.memory_id for m in members if m.memory_id != memory_id
                ],
                summary=summary_text,
            )
            break

    async def _enrich_causal_chain(
        self,
        result: RecallResult,
        nodes: list,
    ) -> None:
        """Populate causal chain edges (supersedes, derived_from, etc.) on a RecallResult."""
        from ncms.domain.models import EdgeType

        causal = result.context.causal_chain
        for node in nodes:
            try:
                edges = await self._store.get_graph_edges(node.id)
            except Exception:
                continue
            for edge in edges:
                et = edge.edge_type
                tid = edge.target_id
                if et == EdgeType.SUPERSEDES and tid not in causal.supersedes:
                    causal.supersedes.append(tid)
                elif et == EdgeType.SUPERSEDED_BY and tid not in causal.superseded_by:
                    causal.superseded_by.append(tid)
                elif et == EdgeType.DERIVED_FROM and tid not in causal.derived_from:
                    causal.derived_from.append(tid)
                elif et == EdgeType.SUPPORTS and tid not in causal.supports:
                    causal.supports.append(tid)
                elif et == EdgeType.CONFLICTS_WITH and tid not in causal.conflicts_with:
                    causal.conflicts_with.append(tid)

    # ── Document profile expansion ─────────────────────────────────

    async def _expand_document_sections(
        self,
        results: list[RecallResult],
        query: str,
        max_sections: int = 3,
    ) -> list[RecallResult]:
        """Expand document profile memories into relevant child sections.

        When a RecallResult has a memory with structured.doc_id, fetches child
        sections from the document store, scores them against the query using
        simple keyword overlap, and adds the top N as DocumentSectionContext
        entries in the RecallResult context.
        """
        if not self._document_service:
            return results

        query_terms = set(query.lower().split())

        for result in results:
            memory = result.memory.memory
            structured = memory.structured
            if not structured or "doc_id" not in structured:
                continue

            doc_id = structured["doc_id"]
            try:
                # Fetch parent document for metadata
                parent_doc = await self._document_service.get_document(doc_id)
                if not parent_doc:
                    continue

                # Fetch child sections
                children = await self._document_service.get_children_documents(doc_id)
                if not children:
                    continue

                # Score sections against query using keyword overlap
                scored_sections: list[tuple[float, int, Any]] = []
                for child in children:
                    child_terms = set(child.content.lower().split())
                    if not child_terms:
                        continue
                    overlap = len(query_terms & child_terms)
                    # Normalize by query length for Jaccard-like score
                    score = overlap / max(len(query_terms), 1)
                    section_idx = (child.metadata or {}).get("section_index", 0)
                    scored_sections.append((score, section_idx, child))

                # Sort by relevance score descending, take top N
                scored_sections.sort(key=lambda x: (-x[0], x[1]))
                top_sections = scored_sections[:max_sections]

                for score, idx, child in top_sections:
                    result.context.document_sections.append(
                        DocumentSectionContext(
                            doc_id=doc_id,
                            doc_title=parent_doc.title,
                            doc_type=parent_doc.doc_type,
                            from_agent=parent_doc.from_agent,
                            section_heading=child.title,
                            section_content=child.content,
                            section_index=idx,
                            relevance_score=score,
                        )
                    )

                logger.info(
                    "[recall] Expanding document profile %s: found %d sections, returning top %d",
                    doc_id, len(children), len(top_sections),
                )
            except Exception as exc:
                logger.warning(
                    "[recall] Failed to expand document profile %s: %s", doc_id, exc,
                )

        return results

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
            results, levels, path = await self._traverse_top_down(
                seed_memory, seed_nodes, limit,
            )
        elif traversal_mode == TraversalMode.BOTTOM_UP:
            results, levels, path = await self._traverse_bottom_up(
                seed_memory, seed_nodes, limit,
            )
        elif traversal_mode == TraversalMode.TEMPORAL:
            results, levels, path = await self._traverse_temporal(
                seed_memory, seed_nodes, limit,
            )
        elif traversal_mode == TraversalMode.LATERAL:
            results, levels, path = await self._traverse_lateral(
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

    async def _traverse_top_down(
        self, seed_memory: Memory, seed_nodes: list, limit: int,
    ) -> tuple[list, int, list]:
        """Abstract → episodes it summarizes → atomic members."""
        results: list[RecallResult] = []
        path: list[str] = [seed_memory.id]
        levels = 0
        seen: set[str] = {seed_memory.id}

        # Level 1: Find episodes this abstract summarizes
        episode_node_ids: list[str] = []
        for node in seed_nodes:
            edges = await self._store.get_graph_edges(node.id)
            for edge in edges:
                if edge.edge_type in (
                    EdgeType.SUMMARIZES, EdgeType.ABSTRACTS,
                ) and edge.target_id not in seen:
                    episode_node_ids.append(edge.target_id)
                    seen.add(edge.target_id)

        if episode_node_ids:
            levels += 1
            for ep_node_id in episode_node_ids[:limit]:
                ep_node = await self._store.get_memory_node(ep_node_id)
                if ep_node:
                    mem = await self._store.get_memory(ep_node.memory_id)
                    if mem and mem.id not in seen:
                        seen.add(mem.id)
                        path.append(mem.id)
                        results.append(RecallResult(
                            memory=ScoredMemory(memory=mem),
                            context=RecallContext(),
                            retrieval_path="top_down:episode",
                        ))

        # Level 2: Atomic members of those episodes
        if episode_node_ids and len(results) < limit:
            levels += 1
            for ep_node_id in episode_node_ids:
                members = await self._store.get_episode_members(ep_node_id)
                for member in members:
                    if len(results) >= limit:
                        break
                    if member.memory_id not in seen:
                        seen.add(member.memory_id)
                        mem = await self._store.get_memory(member.memory_id)
                        if mem:
                            path.append(mem.id)
                            results.append(RecallResult(
                                memory=ScoredMemory(memory=mem),
                                context=RecallContext(),
                                retrieval_path="top_down:atomic",
                            ))

        return results, levels, path

    async def _traverse_bottom_up(
        self, seed_memory: Memory, seed_nodes: list, limit: int,
    ) -> tuple[list, int, list]:
        """Atomic → episode membership → abstract summaries."""
        results: list[RecallResult] = []
        path: list[str] = [seed_memory.id]
        levels = 0
        seen: set[str] = {seed_memory.id}

        # Level 1: Find episode(s) this memory belongs to
        episode_node_ids: list[str] = []
        for node in seed_nodes:
            edges = await self._store.get_graph_edges(node.id)
            for edge in edges:
                if edge.edge_type == EdgeType.BELONGS_TO_EPISODE:
                    episode_node_ids.append(edge.target_id)

        if episode_node_ids:
            levels += 1
            for ep_node_id in episode_node_ids[:limit]:
                ep_node = await self._store.get_memory_node(ep_node_id)
                if ep_node and ep_node.memory_id not in seen:
                    seen.add(ep_node.memory_id)
                    mem = await self._store.get_memory(ep_node.memory_id)
                    if mem:
                        path.append(mem.id)
                        results.append(RecallResult(
                            memory=ScoredMemory(memory=mem),
                            context=RecallContext(),
                            retrieval_path="bottom_up:episode",
                        ))

        # Level 2: Abstracts that summarize those episodes
        if episode_node_ids and len(results) < limit:
            levels += 1
            for ep_node_id in episode_node_ids:
                # Look for incoming SUMMARIZES edges to this episode
                ep_edges = await self._store.get_graph_edges(ep_node_id)
                for edge in ep_edges:
                    if edge.edge_type == EdgeType.SUMMARIZES:
                        abs_node = await self._store.get_memory_node(edge.source_id)
                        if abs_node and abs_node.memory_id not in seen:
                            seen.add(abs_node.memory_id)
                            mem = await self._store.get_memory(abs_node.memory_id)
                            if mem:
                                path.append(mem.id)
                                results.append(RecallResult(
                                    memory=ScoredMemory(memory=mem),
                                    context=RecallContext(),
                                    retrieval_path="bottom_up:abstract",
                                ))

        return results, levels, path

    async def _traverse_temporal(
        self, seed_memory: Memory, seed_nodes: list, limit: int,
    ) -> tuple[list, int, list]:
        """Entity state timeline for entities mentioned in the seed."""
        results: list[RecallResult] = []
        path: list[str] = [seed_memory.id]
        seen: set[str] = {seed_memory.id}

        # Find entities linked to seed memory
        entity_ids = self._graph.get_entity_ids_for_memory(seed_memory.id)

        for entity_id in entity_ids[:5]:  # Cap entities to avoid explosion
            states = await self._store.get_entity_states_by_entity(entity_id)
            # Sort by observed_at for timeline ordering
            states.sort(key=lambda s: s.observed_at or s.created_at)
            for state_node in states:
                if len(results) >= limit:
                    break
                if state_node.memory_id not in seen:
                    seen.add(state_node.memory_id)
                    mem = await self._store.get_memory(state_node.memory_id)
                    if mem:
                        path.append(mem.id)
                        results.append(RecallResult(
                            memory=ScoredMemory(memory=mem),
                            context=RecallContext(),
                            retrieval_path="temporal:state_timeline",
                        ))

        levels = 1 if results else 0
        return results, levels, path

    async def _traverse_lateral(
        self, seed_memory: Memory, seed_nodes: list, limit: int,
    ) -> tuple[list, int, list]:
        """Episode siblings + related episodes via shared entities."""
        results: list[RecallResult] = []
        path: list[str] = [seed_memory.id]
        levels = 0
        seen: set[str] = {seed_memory.id}

        # Level 1: Sibling memories in the same episode(s)
        episode_node_ids: list[str] = []
        for node in seed_nodes:
            edges = await self._store.get_graph_edges(node.id)
            for edge in edges:
                if edge.edge_type == EdgeType.BELONGS_TO_EPISODE:
                    episode_node_ids.append(edge.target_id)

        if episode_node_ids:
            levels += 1
            for ep_node_id in episode_node_ids:
                members = await self._store.get_episode_members(ep_node_id)
                for member in members:
                    if len(results) >= limit:
                        break
                    if member.memory_id not in seen:
                        seen.add(member.memory_id)
                        mem = await self._store.get_memory(member.memory_id)
                        if mem:
                            path.append(mem.id)
                            results.append(RecallResult(
                                memory=ScoredMemory(memory=mem),
                                context=RecallContext(),
                                retrieval_path="lateral:sibling",
                            ))

        # Level 2: Related episodes via shared topic entities
        if episode_node_ids and len(results) < limit:
            levels += 1
            seed_entities: set[str] = set()
            for ep_id in episode_node_ids:
                ep_node = await self._store.get_memory_node(ep_id)
                if ep_node:
                    meta = EpisodeMeta.from_node(ep_node)
                    if meta:
                        seed_entities.update(meta.topic_entities)

            if seed_entities:
                all_episodes = await self._store.get_memory_nodes_by_type("episode")
                for ep in all_episodes:
                    if ep.id in episode_node_ids:
                        continue
                    meta = EpisodeMeta.from_node(ep)
                    if not meta:
                        continue
                    overlap = seed_entities & set(meta.topic_entities)
                    if overlap and ep.memory_id not in seen:
                        seen.add(ep.memory_id)
                        mem = await self._store.get_memory(ep.memory_id)
                        if mem:
                            path.append(mem.id)
                            results.append(RecallResult(
                                memory=ScoredMemory(memory=mem),
                                context=RecallContext(),
                                retrieval_path="lateral:related_episode",
                            ))
                            if len(results) >= limit:
                                break

        return results, levels, path

    async def get_topic_map(self) -> list[TopicCluster]:
        """Generate emergent topic map from L4 abstract clustering.

        Clusters abstract nodes by shared topic_entities using Jaccard
        overlap. Returns topic clusters ordered by size.
        """
        if not self._config.topic_map_enabled:
            return []

        # Gather all abstract nodes
        abstracts = await self._store.get_memory_nodes_by_type("abstract")
        if len(abstracts) < self._config.topic_map_min_abstracts:
            return []

        # Extract entity sets per abstract
        abstract_entities: dict[str, set[str]] = {}
        abstract_episodes: dict[str, list[str]] = {}
        for node in abstracts:
            meta = node.metadata or {}
            entities = set(meta.get("topic_entities", [])
                          or meta.get("key_entities", []))
            if entities:
                abstract_entities[node.memory_id] = entities
                # Track source episodes
                src_eps = meta.get("source_episode_ids", [])
                abstract_episodes[node.memory_id] = src_eps if src_eps else []

        if not abstract_entities:
            return []

        # Greedy clustering by Jaccard overlap
        threshold = self._config.topic_map_entity_overlap
        unclustered = set(abstract_entities.keys())
        clusters: list[TopicCluster] = []

        while unclustered:
            seed_id = next(iter(unclustered))
            unclustered.discard(seed_id)
            cluster_ids = [seed_id]
            cluster_entities = set(abstract_entities[seed_id])

            # Find all abstracts overlapping with cluster
            changed = True
            while changed:
                changed = False
                for mid in list(unclustered):
                    e = abstract_entities[mid]
                    union = cluster_entities | e
                    overlap = cluster_entities & e
                    jaccard = len(overlap) / len(union) if union else 0
                    if jaccard >= threshold:
                        cluster_ids.append(mid)
                        cluster_entities |= e
                        unclustered.discard(mid)
                        changed = True

            if len(cluster_ids) < self._config.topic_map_min_abstracts:
                continue

            # Build label from top entities by frequency
            entity_freq: dict[str, int] = {}
            all_episode_ids: list[str] = []
            for mid in cluster_ids:
                for ent in abstract_entities.get(mid, set()):
                    entity_freq[ent] = entity_freq.get(ent, 0) + 1
                all_episode_ids.extend(abstract_episodes.get(mid, []))

            top_entities = sorted(entity_freq, key=entity_freq.get, reverse=True)[:5]  # type: ignore[arg-type]
            label = " / ".join(top_entities) if top_entities else "Unnamed Topic"

            clusters.append(TopicCluster(
                label=label,
                entity_keys=top_entities,
                abstract_ids=cluster_ids,
                episode_ids=list(set(all_episode_ids)),
                confidence=len(cluster_ids) / len(abstracts),
                member_count=len(cluster_ids),
            ))

        # Sort by size descending
        clusters.sort(key=lambda c: c.member_count, reverse=True)
        logger.info("[topic_map] Generated %d topic clusters from %d abstracts",
                    len(clusters), len(abstracts))
        return clusters

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

    async def _find_episode_summary(self, episode_node_id: str) -> str | None:
        """Find an episode summary abstract that SUMMARIZES this episode."""
        try:
            edges = await self._store.get_graph_edges(episode_node_id)
        except Exception:
            return None
        for edge in edges:
            if edge.edge_type in ("summarizes",):
                # The source of a SUMMARIZES edge is the abstract
                summary_node = await self._store.get_memory_node(edge.source_id)
                if summary_node:
                    memory = await self._store.get_memory(summary_node.memory_id)
                    if memory:
                        return memory.content[:500]
        return None
