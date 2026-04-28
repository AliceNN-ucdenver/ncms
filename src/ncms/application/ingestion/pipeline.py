"""Ingestion pipeline: the full store-memory flow.

Seven stages — each a public method on ``IngestionPipeline``:

1. **pre_admission_gates** — dedup, size check, content classification
2. **gate_admission** — 4-feature admission scoring (discard /
   ephemeral / persist)
3. **run_inline_indexing** — parallel BM25 + SPLADE + configured
   entity extraction + entity linking + co-occurrence edges
4. **create_memory_nodes** — L1 atomic + optional L2 entity_state
5. **reconcile_entity_state** — state reconciliation against existing
   entity states
6. **assign_episode** — hybrid episode linker
7. **deferred_contradiction_check** — post-ingest LLM contradiction
   detection (fire-and-forget)

``extract_entity_state_meta`` is a static helper shared with
``index_worker`` for L2 metadata extraction.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from ncms.application.entity_extraction_mode import (
    slm_slots_to_entity_dicts as _slm_slots_to_entity_dicts,
)
from ncms.application.entity_extraction_mode import (
    use_gliner_entities,
)
from ncms.application.label_cache import load_cached_labels
from ncms.domain.entity_extraction import (
    TEMPORAL_LABELS,
    add_temporal_labels,
    resolve_labels,
)
from ncms.domain.models import Memory, MemoryNode, Relationship
from ncms.domain.temporal.normalizer import (
    RawSpan,
    merge_intervals,
    normalize_spans,
)

if TYPE_CHECKING:
    from ncms.application.admission_service import AdmissionService
    from ncms.application.episode_service import EpisodeService
    from ncms.application.reconciliation_service import (
        ReconciliationService,
    )
    from ncms.application.section_service import SectionService
    from ncms.config import NCMSConfig
    from ncms.domain.protocols import GraphEngine, IndexEngine, MemoryStore
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.observability.event_log import (
        EventLog,
        NullEventLog,
    )

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Store-side gates, indexing, node creation, and reconciliation.

    Holds all ingestion-side dependencies.  Each method takes
    everything it needs as explicit arguments (including
    ``emit_stage``) so that stages can be tested in isolation.
    """

    def __init__(
        self,
        store: MemoryStore,
        index: IndexEngine,
        graph: GraphEngine,
        event_log: EventLog | NullEventLog,
        config: NCMSConfig,
        splade: SpladeEngine | None = None,
        admission: AdmissionService | None = None,
        reconciliation: ReconciliationService | None = None,
        episode: EpisodeService | None = None,
        section_service: SectionService | None = None,
        add_entity: (Callable[..., Awaitable[Any]] | None) = None,
        intent_slot: Any | None = None,
    ) -> None:
        self._store = store
        self._index = index
        self._graph = graph
        self._event_log = event_log
        self._config = config
        self._splade = splade
        self._admission = admission
        self._reconciliation = reconciliation
        self._episode = episode
        self._section_svc = section_service
        self._add_entity = add_entity
        # P2 intent-slot SLM — optional per-deployment classifier
        # unifying admission / state-change / topic / preference.
        # ``None`` → pipeline runs without SLM (pre-P2 behaviour).
        self._intent_slot = intent_slot

    # ── SLM slot-head → entity dicts ─────────────────────────────────────

    @staticmethod
    def slm_slots_to_entity_dicts(
        label: Any | None,
        *,
        confidence_threshold: float = 0.7,
    ) -> list[dict]:
        """Convert SLM slot-head output into the entity-dict format
        the downstream linker consumes.

        The slot head is trained per-adapter on a typed taxonomy
        (e.g. ``library``, ``medication``, ``service``) and — on
        trained domains — beats GLiNER's zero-shot NER in both
        precision and typed-label quality.  We promote its outputs
        to primary and let GLiNER act as the open-vocabulary
        fallback.

        Each ``(slot_name, surface)`` pair in ``label.slots`` becomes
        an entity dict with ``type`` = slot name, ``name`` = surface.
        Entries below ``confidence_threshold`` are dropped so low-
        precision picks don't flood the entity graph.

        Returns an empty list when the label is ``None``, has no
        slots, or every slot is below threshold.
        """
        return _slm_slots_to_entity_dicts(
            label,
            confidence_threshold=confidence_threshold,
        )

    # ── Entity State Extraction (shared with index_worker) ──────────────

    @staticmethod
    def extract_entity_state_meta(
        content: str,
        entities: list[dict],
        slm_label: dict | None = None,
    ) -> dict:
        """Extract entity state metadata from content + SLM output.

        Thin pass-through to :mod:`ncms.application.ingestion.state_meta`.
        See that module's docstring for the strategy chain.
        """
        from ncms.application.ingestion.state_meta import extract_entity_state_meta as _impl

        return _impl(content, entities, slm_label)

    # ── Stage 1: Pre-Admission Gates ────────────────────────────────────

    async def pre_admission_gates(
        self,
        content: str,
        memory_type: str,
        importance: float,
        tags: list[str] | None,
        structured: dict | None,
        source_agent: str | None,
        emit_stage: Callable,
        pipeline_start: float,
        subjects: list | None = None,
        parent_doc_id: str | None = None,
    ) -> Memory | tuple[str, list[str] | None]:
        """Run pre-admission gates: dedup, size check, classification.

        Returns a ``Memory`` for early exit (dedup hit or navigable
        content), or ``(content_hash, updated_tags)`` to continue with
        the atomic admission pipeline.
        """
        # Gate 1: Content-hash dedup
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        try:
            existing = await self._store.get_memory_by_content_hash(
                content_hash,
            )
        except AttributeError:
            existing = None  # Store doesn't support this yet
        if existing is not None:
            logger.info(
                "Dedup: content hash %s already exists as memory %s",
                content_hash[:12],
                existing.id,
            )
            emit_stage(
                "dedup_skip",
                (time.perf_counter() - pipeline_start) * 1000,
                {
                    "existing_memory_id": existing.id,
                    "content_hash": content_hash[:12],
                },
            )
            return existing

        # Gate 2: Content size diagnostic
        max_len = self._config.max_content_length
        if len(content) > max_len:
            logger.info(
                "Content size: %d chars exceeds %d (importance=%.1f) — %s",
                len(content),
                max_len,
                importance,
                "will split via section extraction"
                if self._config.content_classification_enabled
                else "proceeding as atomic (classification disabled)",
            )
            emit_stage(
                "size_flag",
                (time.perf_counter() - pipeline_start) * 1000,
                {
                    "content_length": len(content),
                    "max_content_length": max_len,
                    "importance": importance,
                    "classification_enabled": (self._config.content_classification_enabled),
                },
            )
            if tags is None:
                tags = []
            tags = list(tags) + ["oversized_content"]

        # Gate 3: Content classification (ATOMIC vs NAVIGABLE)
        navigable_memory = await self._maybe_ingest_navigable(
            content,
            memory_type,
            importance,
            tags,
            structured,
            source_agent,
            emit_stage,
            pipeline_start,
            subjects=subjects,
            parent_doc_id=parent_doc_id,
        )
        if navigable_memory is not None:
            return navigable_memory

        return content_hash, tags

    async def _maybe_ingest_navigable(
        self,
        content: str,
        memory_type: str,
        importance: float,
        tags: list[str] | None,
        structured: dict | None,
        source_agent: str | None,
        emit_stage: Callable,
        pipeline_start: float,
        subjects: list | None = None,
        parent_doc_id: str | None = None,
    ) -> Memory | None:
        if not (self._config.content_classification_enabled and self._section_svc is not None):
            return None
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
                    emit_stage(
                        "content_classification",
                        (time.perf_counter() - t0) * 1000,
                        {
                            "content_class": (classification.content_class.value),
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
                        subjects=subjects,
                        parent_doc_id=parent_doc_id,
                    )
            emit_stage(
                "content_classification",
                (time.perf_counter() - t0) * 1000,
                {
                    "content_class": (classification.content_class.value),
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
            emit_stage(
                "content_classification_error",
                (time.perf_counter() - pipeline_start) * 1000,
            )
        return None

    # ── Stage 2: Admission Gate ─────────────────────────────────────────

    async def gate_admission(
        self,
        content: str,
        domains: list[str] | None,
        tags: list[str] | None,
        source_agent: str | None,
        project: str | None,
        memory_type: str,
        importance: float,
        structured: dict | None,
        emit_stage: Callable,
        pipeline_start: float,
        intent_slot_label: Any | None = None,
    ) -> Memory | tuple[str | None, object | None, dict | None]:
        """Run admission scoring.

        When ``intent_slot_label`` is supplied and its
        ``admission_head`` is confident (by the configured threshold),
        the SLM's decision replaces the 4-feature regex heuristic
        entirely — features are still computed (cheap) for logging /
        admission_scored event, but the routing decision comes from
        the classifier.

        Returns a ``Memory`` for early exit (discard / ephemeral) or
        ``(route, features, structured)`` to continue the persist path.
        """
        from dataclasses import asdict as _asdict

        from ncms.domain.scoring import score_admission

        assert self._admission is not None

        t0 = time.perf_counter()
        try:
            features = await self._admission.compute_features(
                content,
                domains=domains,
                source_agent=source_agent,
            )
            score = score_admission(features)
            route, slm_route = self._resolve_admission_route(
                intent_slot_label,
                features,
                score,
            )
            feature_dict = _asdict(features)
            self._emit_admission_event(
                t0=t0,
                score=score,
                route=route,
                slm_route=slm_route,
                feature_dict=feature_dict,
                source_agent=source_agent,
                emit_stage=emit_stage,
            )

            # importance >= 8.0 bypasses admission entirely (force-store)
            if importance >= 8.0:
                logger.debug(
                    "Admission: features computed (state_change=%.2f) "
                    "but routing skipped for high-importance content "
                    "(%.1f)",
                    features.state_change_signal,
                    importance,
                )
                return self._build_persist_continuation(
                    structured,
                    score,
                    "persist",
                    feature_dict,
                    features,
                )

            if route == "discard":
                return self._handle_discard_route(
                    content=content,
                    memory_type=memory_type,
                    domains=domains,
                    tags=tags,
                    source_agent=source_agent,
                    project=project,
                    score=score,
                    emit_stage=emit_stage,
                    pipeline_start=pipeline_start,
                )
            if route == "ephemeral_cache":
                return await self._handle_ephemeral_route(
                    content=content,
                    memory_type=memory_type,
                    domains=domains,
                    tags=tags,
                    source_agent=source_agent,
                    project=project,
                    score=score,
                    emit_stage=emit_stage,
                    pipeline_start=pipeline_start,
                )
            return self._build_persist_continuation(
                structured,
                score,
                route,
                feature_dict,
                features,
            )

        except Exception:
            logger.warning(
                "Admission scoring failed, proceeding without admission",
                exc_info=True,
            )
            emit_stage(
                "admission_error",
                (time.perf_counter() - t0) * 1000,
            )
            return None, None, structured

    def _resolve_admission_route(
        self,
        intent_slot_label: Any | None,
        features: Any,
        score: float,
    ) -> tuple[str, str | None]:
        """Decide admission route: SLM-first, regex fallback.

        Returns ``(route, slm_route)`` — ``slm_route`` is non-None
        only when the SLM drove the decision (used for the
        ``route_source`` field of the dashboard event).
        """
        from ncms.domain.scoring import route_memory

        slm_method = getattr(intent_slot_label, "method", "") or ""
        slm_route: str | None = None
        if (
            intent_slot_label is not None
            and slm_method == "joint_bert_lora"
            and getattr(intent_slot_label, "admission", None) is not None
            and intent_slot_label.is_admission_confident(
                self._config.slm_confidence_threshold,
            )
        ):
            raw = intent_slot_label.admission
            slm_route = "ephemeral_cache" if raw == "ephemeral" else raw
        route = slm_route or route_memory(features, score)
        return route, slm_route

    def _emit_admission_event(
        self,
        *,
        t0: float,
        score: float,
        route: str,
        slm_route: str | None,
        feature_dict: dict,
        source_agent: str | None,
        emit_stage: Callable,
    ) -> None:
        emit_stage(
            "admission",
            (time.perf_counter() - t0) * 1000,
            {
                "score": round(score, 3),
                "route": route,
                "route_source": ("intent_slot" if slm_route is not None else "regex"),
                "features": {k: round(v, 3) for k, v in feature_dict.items()},
            },
        )
        self._event_log.admission_scored(
            memory_id=None,
            score=score,
            route=route,
            features=feature_dict,
            agent_id=source_agent,
        )

    def _handle_discard_route(
        self,
        *,
        content: str,
        memory_type: str,
        domains: list[str] | None,
        tags: list[str] | None,
        source_agent: str | None,
        project: str | None,
        score: float,
        emit_stage: Callable,
        pipeline_start: float,
    ) -> Memory:
        """Admission-discard early exit.  Memory is NOT persisted."""
        logger.info(
            "Admission: discarding content (score=%.3f)",
            score,
        )
        emit_stage(
            "complete",
            (time.perf_counter() - pipeline_start) * 1000,
            {
                "result": "discarded",
                "admission_score": round(score, 3),
            },
        )
        return Memory(
            content=content,
            type=cast(Any, memory_type),
            domains=domains or [],
            tags=tags or [],
            source_agent=source_agent,
            project=project,
            structured={
                "admission": {"score": score, "route": "discard"},
            },
        )

    async def _handle_ephemeral_route(
        self,
        *,
        content: str,
        memory_type: str,
        domains: list[str] | None,
        tags: list[str] | None,
        source_agent: str | None,
        project: str | None,
        score: float,
        emit_stage: Callable,
        pipeline_start: float,
    ) -> Memory:
        """Admission-ephemeral early exit.

        Saves an ``EphemeralEntry`` with TTL but does NOT promote
        to the persistent ``memories`` table.  Returns a synthetic
        Memory carrying the ephemeral_id in structured metadata.
        """
        from ncms.domain.models import EphemeralEntry

        ttl = self._config.admission_ephemeral_ttl_seconds
        now = datetime.now(UTC)
        entry = EphemeralEntry(
            content=content,
            source_agent=source_agent,
            domains=domains or [],
            admission_score=score,
            ttl_seconds=ttl,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        await self._store.save_ephemeral(entry)
        logger.info(
            "Admission: ephemeral cache (score=%.3f, ttl=%ds)",
            score,
            ttl,
        )
        emit_stage(
            "complete",
            (time.perf_counter() - pipeline_start) * 1000,
            {
                "result": "ephemeral",
                "admission_score": round(score, 3),
                "ephemeral_id": entry.id,
            },
        )
        return Memory(
            content=content,
            type=cast(Any, memory_type),
            domains=domains or [],
            tags=tags or [],
            source_agent=source_agent,
            project=project,
            structured={
                "admission": {
                    "score": score,
                    "route": "ephemeral_cache",
                    "ephemeral_id": entry.id,
                }
            },
        )

    @staticmethod
    def _build_persist_continuation(
        structured: dict | None,
        score: float,
        route: str | None,
        feature_dict: dict,
        features: object,
    ) -> tuple[str | None, object, dict]:
        """Attach admission features to ``structured`` for the persist path."""
        result = dict(structured or {})
        result["admission"] = {
            "score": round(score, 3),
            "route": route,
            **{k: round(v, 3) for k, v in feature_dict.items()},
        }
        return route, features, result

    # ── Stage 3: Inline Indexing ────────────────────────────────────────

    async def run_intent_slot_extraction(
        self,
        content: str,
        *,
        domain: str = "",
    ) -> Any | None:
        """Run the SLM on ``content`` and return the ExtractedLabel.

        **Pure function** — no side effects.  The caller
        (:meth:`MemoryService.store_memory`) is responsible for:

        * Baking the label into ``memory.structured["intent_slot"]``
          BEFORE ``save_memory`` so the column-persistence path
          writes everything in one pass.
        * Passing the label into the admission gate so the SLM's
          ``admission_head`` replaces the regex heuristic when
          confident.
        * Appending ``topic`` to ``Memory.domains`` when
          ``is_topic_confident`` + ``slm_populate_domains``.
        * Calling :meth:`MemoryStore.save_memory_slots` after
          ``save_memory`` to persist slot surface forms.
        * Emitting the ``intent_slot.extracted`` dashboard event.

        Returns ``None`` when no extractor is wired, or the
        extractor raises.  Callers treat ``None`` as "no SLM
        signal — use legacy regex paths".  The previous
        ``slm_enabled`` config flag was deleted (Phase I.6
        completion); the chain's presence is the kill-switch
        now — pass ``intent_slot=None`` to MemoryService to
        keep the SLM dark.

        Runs the classifier on a thread pool so async callers
        don't block the event loop on a ~20-65ms forward pass.
        """
        if self._intent_slot is None:
            return None

        t0 = time.perf_counter()
        try:
            label = await asyncio.to_thread(
                self._intent_slot.extract,
                content,
                domain=domain,
            )
        except Exception:
            logger.warning(
                "[intent_slot] extraction failed — continuing without labels",
                exc_info=True,
            )
            return None
        # Annotate latency on the label so downstream persistence
        # captures the wall-time of THIS backend's forward pass
        # (distinct from the pipeline stage emit timing).
        label.latency_ms = (time.perf_counter() - t0) * 1000.0
        return label

    async def run_inline_indexing(
        self,
        memory: Memory,
        content: str,
        domains: list[str] | None,
        entities_manual: list[dict] | None,
        emit_stage: Callable,
        slot_entities_present: bool = False,
    ) -> tuple[list[dict], list[str]]:
        """Run BM25, SPLADE, and configured entity extraction; link entities.

        ``slot_entities_present`` is passed through for observability and
        compatibility.  The configured entity extraction lane decides
        whether GLiNER can run at all.

        Returns ``(all_entities, linked_entity_ids)``.
        """
        async def _do_bm25() -> float:
            t = time.perf_counter()
            await asyncio.to_thread(
                self._index.index_memory,
                memory,
            )
            return (time.perf_counter() - t) * 1000

        async def _do_splade() -> float:
            if self._splade is None:
                return 0.0
            t = time.perf_counter()
            try:
                await asyncio.to_thread(
                    self._splade.index_memory,
                    memory,
                )
            except Exception:
                logger.warning(
                    "SPLADE indexing failed for %s, continuing",
                    memory.id,
                    exc_info=True,
                )
            return (time.perf_counter() - t) * 1000

        async def _do_gliner() -> tuple[list[dict[str, Any]], float]:
            if not use_gliner_entities(self._config):
                return [], 0.0
            from ncms.infrastructure.extraction.gliner_extractor import (
                extract_with_label_budget,
            )

            t = time.perf_counter()
            cached = await load_cached_labels(self._store, domains or [])
            gliner_labels = resolve_labels(
                domains or [],
                cached_labels=cached,
            )
            # P1-temporal-experiment: additively merge temporal labels
            # for content-date extraction at ingest (§2.1 of the design).
            if self._config.temporal_range_filter_enabled:
                gliner_labels = add_temporal_labels(gliner_labels)
            result = await asyncio.to_thread(
                extract_with_label_budget,
                content,
                gliner_labels,
                model_name=self._config.gliner_model,
                threshold=self._config.gliner_threshold,
                cache_dir=self._config.model_cache_dir,
            )
            return result, (time.perf_counter() - t) * 1000

        # Intent-slot SLM is NOT invoked here — it runs earlier in
        # MemoryService.store_memory so its admission + state-change
        # heads can gate the ingest path.  Indexing only cares about
        # BM25 / SPLADE / configured entity extraction, which remain
        # parallel.
        logger.info(
            "[store] Starting parallel indexing: BM25 + SPLADE + entity_extraction(%s)",
            self._config.entity_extraction_mode,
        )
        bm25_ms, splade_ms, (auto_entities, extract_ms) = await asyncio.gather(
            _do_bm25(),
            _do_splade(),
            _do_gliner(),
        )
        logger.info(
            "[store] Parallel indexing complete: BM25=%.0fms SPLADE=%.0fms entity=%.0fms",
            bm25_ms,
            splade_ms,
            extract_ms,
        )

        emit_stage("bm25_index", bm25_ms, memory_id=memory.id)
        if self._splade is not None:
            emit_stage("splade_index", splade_ms, memory_id=memory.id)

        # P1-temporal-experiment: split temporal spans out of the
        # GLiNER output before entity linking, resolve them to a
        # content range, and persist when non-empty.  The entity-
        # linking path below sees only entity-typed items.
        auto_entities = await self._persist_content_range(
            memory,
            auto_entities,
            emit_stage,
        )

        # Merge manual + auto-extracted entities (dedup by name)
        manual = list(entities_manual or [])
        manual_names = {e["name"].lower() for e in manual}
        all_entities = manual + [e for e in auto_entities if e["name"].lower() not in manual_names]
        emit_stage(
            "entity_extraction",
            extract_ms,
            {
                "extractor": (
                    "gliner" if use_gliner_entities(self._config) else "slm_structured"
                ),
                "mode": self._config.entity_extraction_mode,
                "slm_entities_present": slot_entities_present,
                "auto_count": len(auto_entities),
                "manual_count": len(manual),
                "total_count": len(all_entities),
                "entity_names": [e["name"] for e in all_entities[:10]],
            },
            memory_id=memory.id,
        )

        # Link entities to memory in graph + store
        t0 = time.perf_counter()
        linked_entity_ids: list[str] = []
        assert self._add_entity is not None
        for e_data in all_entities:
            entity = await self._add_entity(
                name=e_data["name"],
                entity_type=e_data.get("type", "concept"),
                attributes=e_data.get("attributes", {}),
            )
            linked_entity_ids.append(entity.id)
            await self._store.link_memory_entity(memory.id, entity.id)
            self._graph.link_memory_entity(memory.id, entity.id)
        emit_stage(
            "graph_linking",
            (time.perf_counter() - t0) * 1000,
            {
                "entities_linked": len(all_entities),
            },
            memory_id=memory.id,
        )

        # Co-occurrence edges
        if len(linked_entity_ids) > 1:
            self.build_cooccurrence_edges(
                memory.id,
                linked_entity_ids,
                emit_stage,
            )

        return all_entities, linked_entity_ids

    async def _persist_content_range(
        self,
        memory: Memory,
        auto_entities: list[dict[str, str]],
        emit_stage: Callable,
    ) -> list[dict[str, str]]:
        """Split temporal spans out of GLiNER output, resolve, persist.

        Returns the entity-only subset of ``auto_entities`` (temporal
        items removed).  When the feature flag is off, passes through
        unchanged.

        Resolution order (§14.2 of the design):
          1. If GLiNER extracted content-date spans that normalize to
             at least one interval, persist the merged content range
             with ``source='gliner'``.
          2. Otherwise, fall back to ``memory.observed_at`` (session
             envelope date) as a day-wide range with
             ``source='metadata'``.  This gives ~100% memory coverage
             on benchmarks like LongMemEval where conversational prose
             rarely contains explicit dates but every session carries
             a timestamp.
          3. If neither is available, persist nothing — retrieval will
             treat the memory as range-unknown.

        P1-temporal-experiment, Phase A (revised).  See
        ``docs/retired/p1-temporal-experiment.md`` §14.
        """
        if not self._config.temporal_range_filter_enabled:
            return auto_entities
        temporal_label_set = {t.lower() for t in TEMPORAL_LABELS}
        entities_only: list[dict[str, str]] = []
        spans: list[RawSpan] = []
        for item in auto_entities:
            label = str(item.get("type", "")).lower()
            if label in temporal_label_set:
                spans.append(
                    RawSpan(
                        text=str(item.get("name", "")),
                        label=label,
                        char_start=int(item.get("char_start", 0) or 0),
                        char_end=int(item.get("char_end", 0) or 0),
                    )
                )
            else:
                entities_only.append(item)

        # Resolve relative expressions against the memory's own
        # observed_at if set — this lets historical replays encode
        # "yesterday" relative to the session date, not wall clock.
        ref = memory.observed_at or memory.created_at
        intervals = normalize_spans(spans, ref) if spans else []
        merged = merge_intervals(intervals)

        source, range_start, range_end = self._resolve_memory_range(
            merged,
            memory,
        )
        emit_stage(
            "content_range_extracted",
            0.0,
            {
                "span_count": len(spans),
                "resolved_intervals": len(intervals),
                "spans": [s.text for s in spans[:10]],
                "source": source,
                "range_start": range_start,
                "range_end": range_end,
            },
            memory_id=memory.id,
        )
        if range_start is not None and range_end is not None:
            await self._store.save_content_range(
                memory_id=memory.id,
                range_start=range_start,
                range_end=range_end,
                span_count=len(spans),
                source=source or "unknown",
            )
        return entities_only

    @staticmethod
    def _resolve_memory_range(
        merged: object | None,
        memory: Memory,
    ) -> tuple[str | None, str | None, str | None]:
        """Pick the best available range for a memory.

        Returns ``(source, range_start, range_end)`` where range_start
        and range_end are ISO-8601 strings (or all ``None`` when no
        temporal information is available).
        """
        if merged is not None:
            return (
                "gliner",
                merged.start.isoformat(),  # type: ignore[attr-defined]
                merged.end.isoformat(),  # type: ignore[attr-defined]
            )
        anchor = memory.observed_at or memory.created_at
        if anchor is None:
            return None, None, None
        # Day-wide interval anchored on the session timestamp.
        day_start = datetime(
            anchor.year,
            anchor.month,
            anchor.day,
            tzinfo=anchor.tzinfo or UTC,
        )
        day_end = day_start + timedelta(days=1)
        return "metadata", day_start.isoformat(), day_end.isoformat()

    def build_cooccurrence_edges(
        self,
        memory_id: str,
        linked_entity_ids: list[str],
        emit_stage: Callable,
    ) -> None:
        """Build co-occurrence edges between entities in the same memory."""
        t0 = time.perf_counter()
        cooc_ids = linked_entity_ids[: self._config.cooccurrence_max_entities]
        edges_new = 0
        edges_incremented = 0
        for i, a in enumerate(cooc_ids):
            for b in cooc_ids[i + 1 :]:
                existing_count = self._graph.get_edge_cooccurrence(a, b)
                if existing_count > 0:
                    self._graph.increment_edge_cooccurrence(a, b)
                    self._graph.increment_edge_cooccurrence(b, a)
                    edges_incremented += 1
                else:
                    rel_ab = Relationship(
                        source_entity_id=a,
                        target_entity_id=b,
                        type="co_occurs",
                        source_memory_id=memory_id,
                    )
                    rel_ba = Relationship(
                        source_entity_id=b,
                        target_entity_id=a,
                        type="co_occurs",
                        source_memory_id=memory_id,
                    )
                    self._graph.add_relationship(rel_ab)
                    self._graph.add_relationship(rel_ba)
                    edges_new += 1
        emit_stage(
            "cooccurrence_edges",
            (time.perf_counter() - t0) * 1000,
            {
                "edges_new": edges_new,
                "edges_incremented": edges_incremented,
                "entities_used": len(cooc_ids),
                "entities_capped": (
                    len(linked_entity_ids) > self._config.cooccurrence_max_entities
                ),
            },
            memory_id=memory_id,
        )

    # ── Stage 4: Node Creation (L1/L2) ──────────────────────────────────

    async def create_memory_nodes(
        self,
        memory: Memory,
        content: str,
        all_entities: list[dict],
        linked_entity_ids: list[str],
        admission_features: object | None,
        emit_stage: Callable,
        subject: str | None = None,
    ) -> None:
        """Create HTMG nodes for a persisted memory.

        L1 ATOMIC node is always created.  L2 ENTITY_STATE node is
        additionally created if state change or declaration is
        detected, OR when ``subject`` is supplied (caller-asserted
        entity-subject, Option D' Part 4).  Then reconcile against
        existing states and assign to an episode.
        """
        from ncms.domain.models import MemoryNode, NodeType

        # L1: always create atomic node.  Carry observed_at from the
        # Memory so temporal scoring can match against the event's
        # original date rather than NCMS's ingest date.
        l1_node = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ATOMIC,
            importance=memory.importance,
            observed_at=memory.observed_at,
        )
        await self._store.save_memory_node(l1_node)
        emit_stage(
            "memory_node",
            0.0,
            {
                "node_id": l1_node.id,
                "node_type": "atomic",
                "layer": "L1",
            },
            memory_id=memory.id,
        )

        # L2: Detect state change or declaration (or use caller subject).
        # Phase A sub-PR 4: returns a list — multi-subject ingest can
        # produce more than one L2 (one per affected timeline).  Each
        # gets its own reconciliation pass below.
        from ncms.application.ingestion.l2_detection import detect_and_create_l2_node

        l2_nodes = await detect_and_create_l2_node(
            store=self._store,
            config=self._config,
            extract_entity_state_meta_fn=self.extract_entity_state_meta,
            memory=memory,
            content=content,
            all_entities=all_entities,
            l1_node=l1_node,
            admission_features=admission_features,
            emit_stage=emit_stage,
            subject=subject,
        )

        # Reconcile every L2 entity_state node independently — each
        # subject's timeline reconciles against its own prior states.
        if (
            self._reconciliation is not None
            and self._config.temporal_enabled
        ):
            for node in l2_nodes:
                if node.metadata.get("entity_id"):
                    await self._reconcile_entity_state(
                        node,
                        memory.id,
                        emit_stage,
                    )

        # Single-L2 view used by the causal-edges step below (it
        # only consumes the primary L2 today; multi-subject causal
        # edges are out of scope for sub-PR 4).
        primary_l2: MemoryNode | None = l2_nodes[0] if l2_nodes else None

        # Episode formation (links to L1 atomic node)
        if self._episode is not None and self._config.temporal_enabled:
            await self._assign_episode(
                l1_node,
                memory,
                content,
                linked_entity_ids,
                emit_stage,
            )

        # CTLG v8+: extract causal edges from memory-voice cue tags.
        # No-op for v7.x adapters (cue_tags empty).  Gated on
        # temporal_enabled — same flag that governs reconciliation.
        if self._config.temporal_enabled:
            from ncms.application.ingestion.causal_edges import (
                extract_and_persist_causal_edges,
            )

            await extract_and_persist_causal_edges(
                store=self._store,
                config=self._config,
                memory=memory,
                l1_node=l1_node,
                l2_node=primary_l2,
                emit_stage=emit_stage,
            )

    # ── Stage 5: Reconciliation ─────────────────────────────────────────

    async def _reconcile_entity_state(
        self,
        l2_node: MemoryNode,
        memory_id: str,
        emit_stage: Callable,
    ) -> None:
        """Reconcile an L2 entity_state node against existing states."""
        assert self._reconciliation is not None
        t0 = time.perf_counter()
        try:
            results = await self._reconciliation.reconcile(l2_node)
            emit_stage(
                "reconciliation",
                (time.perf_counter() - t0) * 1000,
                {
                    "node_id": l2_node.id,
                    "results_count": len(results),
                    "relations": [
                        {
                            "relation": r.relation,
                            "existing": r.existing_node_id,
                        }
                        for r in results
                    ],
                },
                memory_id=memory_id,
            )
        except Exception:
            logger.warning(
                "Reconciliation failed for node %s, continuing",
                l2_node.id,
                exc_info=True,
            )
            emit_stage(
                "reconciliation_error",
                (time.perf_counter() - t0) * 1000,
                memory_id=memory_id,
            )

    # ── Stage 6: Episode Assignment ─────────────────────────────────────

    async def _assign_episode(
        self,
        l1_node: MemoryNode,
        memory: Memory,
        content: str,
        linked_entity_ids: list[str],
        emit_stage: Callable,
    ) -> None:
        """Assign a memory's L1 node to an episode."""
        assert self._episode is not None
        t0 = time.perf_counter()
        try:
            episode_node = await self._episode.assign_or_create(
                fragment_node=l1_node,
                fragment_memory=memory,
                entity_ids=linked_entity_ids,
            )
            emit_stage(
                "episode_formation",
                (time.perf_counter() - t0) * 1000,
                {
                    "node_id": l1_node.id,
                    "episode_id": (episode_node.id if episode_node else None),
                    "action": "created" if episode_node else "none",
                },
                memory_id=memory.id,
            )

            if episode_node is not None:
                await self._episode.check_resolution_closure(
                    content,
                    episode_node,
                )
        except Exception:
            logger.warning(
                "Episode formation failed for node %s, continuing",
                l1_node.id,
                exc_info=True,
            )
            emit_stage(
                "episode_formation_error",
                (time.perf_counter() - t0) * 1000,
                memory_id=memory.id,
            )

    # ── Stage 6.5: CTLG causal-edge extraction (v8+) ───────────────────

    # ── Stage 7: Deferred Contradiction Detection ──────────────────────

    async def deferred_contradiction_check(
        self,
        memory: Memory,
        all_entities: list[dict],
        pipeline_id: str,
        source_agent: str | None = None,
    ) -> None:
        """Run contradiction detection as a post-ingest background task.

        Memory is already stored and indexed.  If contradictions are
        found, annotates the new memory and existing memories with
        metadata and emits a pipeline event.  Entirely non-fatal.
        """
        t0 = time.perf_counter()
        contradiction_count = 0
        candidates_checked = 0
        try:
            from ncms.infrastructure.llm.contradiction_detector import (
                detect_contradictions,
            )

            # Find similar existing memories
            candidates = self._index.search(
                memory.content,
                limit=self._config.contradiction_candidate_limit + 1,
            )
            candidate_ids = [mid for mid, _ in candidates if mid != memory.id]
            candidate_ids = candidate_ids[: self._config.contradiction_candidate_limit]

            # Also pull in graph-related memories via shared entities
            for e_data in all_entities[:5]:
                eid = self._graph.find_entity_by_name(e_data["name"])
                if eid:
                    related = self._graph.get_related_memory_ids(
                        [eid],
                        depth=1,
                    )
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
                    await self._apply_contradiction_annotations(
                        memory,
                        contradictions,
                    )
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
            pipeline_id=pipeline_id,
            pipeline_type="store",
            stage="contradiction_deferred",
            duration_ms=(time.perf_counter() - t0) * 1000,
            data={
                "candidates_checked": candidates_checked,
                "contradictions_found": contradiction_count,
            },
            agent_id=source_agent,
            memory_id=memory.id,
        )

    async def _apply_contradiction_annotations(
        self,
        memory: Memory,
        contradictions: list[dict],
    ) -> None:
        """Annotate new + existing memories with contradiction metadata."""
        # Annotate the new memory
        structured_data = dict(memory.structured or {})
        structured_data["contradictions"] = contradictions
        memory.structured = structured_data
        await self._store.update_memory(memory)

        # Annotate each contradicted existing memory
        for c in contradictions:
            existing = await self._store.get_memory(
                c["existing_memory_id"],
            )
            if existing:
                ex_structured = dict(existing.structured or {})
                ex_contradictions = ex_structured.get(
                    "contradicted_by",
                    [],
                )
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
