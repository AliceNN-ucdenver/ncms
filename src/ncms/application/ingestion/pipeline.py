"""Ingestion pipeline: the full store-memory flow.

Seven stages — each a public method on ``IngestionPipeline``:

1. **pre_admission_gates** — dedup, size check, content classification
2. **gate_admission** — 4-feature admission scoring (discard /
   ephemeral / persist)
3. **run_inline_indexing** — parallel BM25 + SPLADE + GLiNER +
   entity linking + co-occurrence edges
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
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

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
        add_entity: (
            Callable[..., Awaitable[Any]] | None
        ) = None,
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
        if label is None:
            return []
        slots = getattr(label, "slots", None) or {}
        confidences = getattr(label, "slot_confidences", None) or {}
        out: list[dict] = []
        for slot_name, surface in slots.items():
            if not surface:
                continue
            conf = float(confidences.get(slot_name, 1.0))
            if conf < confidence_threshold:
                continue
            out.append({
                "name": str(surface).strip(),
                "type": str(slot_name),
                "attributes": {
                    "source": "slm_slot",
                    "confidence": round(conf, 3),
                },
            })
        return out

    # ── Entity State Extraction (shared with index_worker) ──────────────

    @staticmethod
    def extract_entity_state_meta(
        content: str,
        entities: list[dict],
        slm_label: dict | None = None,
    ) -> dict:
        """Extract entity state metadata from content + SLM output.

        **v7+ preferred path** — when ``slm_label["role_spans"]``
        contains a ``primary``-role entry, use the span's catalog
        canonical (e.g. ``"postgresql"``) as ``state_value`` and its
        catalog slot (``"database"``, ``"framework"``, ...) as
        ``state_key``.  This gives reconciliation a stable comparison
        key so supersedes/refines edges can actually fire.  Falls
        back to the legacy regex patterns when role_spans is empty
        or doesn't contain a primary.

        Heuristic patterns (tried in order, post-SLM):

        1. ``EntityName: key = value`` — structured assignment
        2. ``EntityName key changed/updated from X to Y`` — transition
        3. ``EntityName: key changed/updated from X to Y`` — colon + transition
        4. ``EntityName key is/was/set to value`` — declaration
        5. Markdown ``## Status\\n\\nvalue`` (ADRs, design docs)
        6. YAML ``status: value`` (checklists, config)
        7. Fallback: first GLiNER entity, first assignment-like line

        Returns a dict suitable for ``MemoryNode.metadata`` with
        ``entity_id``, ``state_key``, ``state_value``, and optionally
        ``state_previous``, ``state_scope``, ``state_alternative``,
        ``source`` (where this metadata came from).
        """
        # ── v7+ SLM role-span path (preferred) ──────────────────────
        # When the role head produced a primary-role catalog hit we
        # use its canonical form as state_value and its slot as
        # state_key.  The entity_id comes from the first GLiNER
        # entity (the memory's subject) or from the caller.  When
        # role_spans also contains an alternative, record it too —
        # reconciliation can use it to verify the supersession.
        if slm_label and slm_label.get("role_spans"):
            role_spans = slm_label["role_spans"]
            primary = next(
                (r for r in role_spans if r.get("role") == "primary"),
                None,
            )
            if primary:
                alt = next(
                    (r for r in role_spans if r.get("role") == "alternative"),
                    None,
                )
                # Pick the subject entity — prefer a GLiNER entity
                # that isn't itself the primary/alternative canonical
                # (so the state belongs to something other than the
                # value that CHANGED).  Example narrative:
                # "auth-service migrated from PostgreSQL to CockroachDB"
                # — GLiNER finds {auth-service, PostgreSQL, CockroachDB};
                # primary=CockroachDB, alt=PostgreSQL, so subject
                # should be auth-service.
                primary_canon = primary["canonical"].lower()
                alt_canon = alt["canonical"].lower() if alt else None
                subject_entity = None
                for ent in entities:
                    name_l = ent["name"].lower()
                    if name_l == primary_canon:
                        continue
                    if alt_canon and name_l == alt_canon:
                        continue
                    subject_entity = ent["name"]
                    break
                meta = {
                    "entity_id": subject_entity or primary["canonical"],
                    "state_key": primary["slot"],
                    "state_value": primary["canonical"],
                    "source": "slm_role_span",
                }
                if alt:
                    meta["state_previous"] = alt["canonical"]
                    meta["state_alternative"] = alt["canonical"]
                return meta

        # ── Legacy regex patterns (fallback when no role_spans) ────
        # Pattern 1: "EntityName: key = value"
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
        p_transition = re.compile(
            r"^([a-zA-Z0-9_\-]+)\s+([a-zA-Z0-9_\-]+)\s+"
            r"(?:changed|updated)\s+from\s+(.+?)\s+to\s+(.+?)"
            r"(?:\s+(?:due|for|after|because)\b.*)?$",
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
        p_colon_transition = re.compile(
            r"^([a-zA-Z0-9_\-]+)\s*:\s*([a-zA-Z0-9_\-]+)\s+"
            r"(?:changed|updated)\s+from\s+(.+?)\s+to\s+(.+?)"
            r"(?:\s+(?:due|for|after|because|per)\b.*)?$",
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

        # Pattern 5: Markdown "## Status\n\nvalue" (ADR documents)
        p_md_status = re.compile(
            r"^#\s+(.+?)$.*?^##?\s*[Ss]tatus\s*$\s*^(\w[\w\s]*)$",
            re.MULTILINE | re.DOTALL,
        )
        m = p_md_status.search(content)
        if m and entities:
            title = m.group(1).strip()
            status_val = m.group(2).strip()
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

        # Pattern 6: YAML "status: value"
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

        # Fallback: first entity + first assignment-like line
        if entities:
            best_line = ""
            for line in content.splitlines():
                stripped = line.strip()
                if stripped and any(
                    c in stripped for c in ("=", ":", "→")
                ):
                    best_line = stripped[:200]
                    break
            if not best_line:
                best_line = (
                    content.splitlines()[0].strip()[:200]
                    if content else ""
                )
            return {
                "entity_id": entities[0]["name"],
                "state_key": "state",
                "state_value": best_line,
            }

        return {}

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
                content_hash[:12], existing.id,
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
                "Content size: %d chars exceeds %d "
                "(importance=%.1f) — %s",
                len(content), max_len, importance,
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
                    "classification_enabled": (
                        self._config.content_classification_enabled
                    ),
                },
            )
            if tags is None:
                tags = []
            tags = list(tags) + ["oversized_content"]

        # Gate 3: Content classification (ATOMIC vs NAVIGABLE)
        navigable_memory = await self._maybe_ingest_navigable(
            content, memory_type, importance, tags, structured,
            source_agent, emit_stage, pipeline_start,
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
    ) -> Memory | None:
        if not (
            self._config.content_classification_enabled
            and self._section_svc is not None
        ):
            return None
        try:
            from ncms.domain.content_classifier import (
                ContentClass,
                classify_content,
                extract_sections,
            )

            t0 = time.perf_counter()
            classification = classify_content(content, memory_type)
            if (
                classification.content_class == ContentClass.NAVIGABLE
            ):
                sections = extract_sections(content, classification)
                if len(sections) >= 2:
                    emit_stage(
                        "content_classification",
                        (time.perf_counter() - t0) * 1000,
                        {
                            "content_class": (
                                classification.content_class.value
                            ),
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
            emit_stage(
                "content_classification",
                (time.perf_counter() - t0) * 1000,
                {
                    "content_class": (
                        classification.content_class.value
                    ),
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
        ``admission_head`` is confident (by the configured
        threshold), the SLM's decision replaces the 4-feature
        regex heuristic entirely — features are still computed
        (cheap) for logging / admission_scored event, but the
        routing decision comes from the classifier.

        Returns a ``Memory`` for early exit (discard / ephemeral) or
        ``(route, features, structured)`` to continue the persist path.
        """
        from dataclasses import asdict as _asdict

        from ncms.domain.models import EphemeralEntry
        from ncms.domain.scoring import route_memory, score_admission

        assert self._admission is not None

        _skip_routing = importance >= 8.0

        t0 = time.perf_counter()
        try:
            features = await self._admission.compute_features(
                content, domains=domains, source_agent=source_agent,
            )
            score = score_admission(features)

            # SLM-first routing.  When the classifier is confident
            # on the admission head we trust its output; otherwise
            # fall through to the regex-based ``route_memory``.
            # This replaces the 4-feature heuristic on the hot path
            # while keeping it available for cold-start fallback.
            slm_route: str | None = None
            if intent_slot_label is not None and getattr(
                intent_slot_label, "admission", None,
            ) is not None and intent_slot_label.is_admission_confident(
                self._config.slm_confidence_threshold,
            ):
                # Normalise label values ("ephemeral" ↔ "ephemeral_cache")
                raw = intent_slot_label.admission
                slm_route = (
                    "ephemeral_cache" if raw == "ephemeral" else raw
                )
            route = slm_route if slm_route is not None else route_memory(
                features, score,
            )

            feature_dict = _asdict(features)
            emit_stage("admission", (time.perf_counter() - t0) * 1000, {
                "score": round(score, 3), "route": route,
                "route_source": (
                    "intent_slot" if slm_route is not None else "regex"
                ),
                "features": {
                    k: round(v, 3) for k, v in feature_dict.items()
                },
            })
            self._event_log.admission_scored(
                memory_id=None, score=score, route=route,
                features=feature_dict, agent_id=source_agent,
            )

            if _skip_routing:
                route = None
                logger.debug(
                    "Admission: features computed (state_change=%.2f) "
                    "but routing skipped for high-importance content "
                    "(%.1f)",
                    features.state_change_signal, importance,
                )
            elif route == "discard":
                logger.info(
                    "Admission: discarding content (score=%.3f)", score,
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
                    content=content, type=cast(Any, memory_type),
                    domains=domains or [], tags=tags or [],
                    source_agent=source_agent, project=project,
                    structured={
                        "admission": {"score": score, "route": "discard"},
                    },
                )
            elif route == "ephemeral_cache":
                ttl = self._config.admission_ephemeral_ttl_seconds
                now = datetime.now(UTC)
                entry = EphemeralEntry(
                    content=content, source_agent=source_agent,
                    domains=domains or [], admission_score=score,
                    ttl_seconds=ttl, created_at=now,
                    expires_at=now + timedelta(seconds=ttl),
                )
                await self._store.save_ephemeral(entry)
                logger.info(
                    "Admission: ephemeral cache (score=%.3f, ttl=%ds)",
                    score, ttl,
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
            emit_stage(
                "admission_error", (time.perf_counter() - t0) * 1000,
            )
            return None, None, structured

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

        Returns ``None`` when the feature flag is off, no
        extractor is wired, or the extractor raises.  Callers
        treat ``None`` as "no SLM signal — use legacy regex paths".

        Runs the classifier on a thread pool so async callers
        don't block the event loop on a ~20-65ms forward pass.
        """
        if self._intent_slot is None or not getattr(
            self._config, "slm_enabled", False,
        ):
            return None

        t0 = time.perf_counter()
        try:
            label = await asyncio.to_thread(
                self._intent_slot.extract,
                content, domain=domain,
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
        skip_gliner: bool = False,
    ) -> tuple[list[dict], list[str]]:
        """Run BM25, SPLADE, GLiNER in parallel; link entities.

        When ``skip_gliner`` is True, the SLM slot head already
        produced confident typed entities for this memory and
        ``entities_manual`` contains them; GLiNER is skipped to
        keep the entity graph clean on trained-domain deployments.

        Returns ``(all_entities, linked_entity_ids)``.
        """
        from ncms.infrastructure.extraction.gliner_extractor import (
            extract_with_label_budget,
        )

        async def _do_bm25() -> float:
            t = time.perf_counter()
            await asyncio.to_thread(
                self._index.index_memory, memory,
            )
            return (time.perf_counter() - t) * 1000

        async def _do_splade() -> float:
            if self._splade is None:
                return 0.0
            t = time.perf_counter()
            try:
                await asyncio.to_thread(
                    self._splade.index_memory, memory,
                )
            except Exception:
                logger.warning(
                    "SPLADE indexing failed for %s, continuing",
                    memory.id, exc_info=True,
                )
            return (time.perf_counter() - t) * 1000

        async def _do_gliner() -> tuple[list[dict[str, str]], float]:
            # SLM slot head primary / GLiNER fallback: when the
            # caller upstream told us the slot head already produced
            # confident typed entities, skip the open-vocabulary NER
            # pass entirely for this memory.
            if skip_gliner:
                return [], 0.0
            t = time.perf_counter()
            cached = await load_cached_labels(self._store, domains or [])
            gliner_labels = resolve_labels(
                domains or [], cached_labels=cached,
            )
            # P1-temporal-experiment: additively merge temporal labels
            # for content-date extraction at ingest (§2.1 of the design).
            if self._config.temporal_range_filter_enabled:
                gliner_labels = add_temporal_labels(gliner_labels)
            result = await asyncio.to_thread(
                extract_with_label_budget, content, gliner_labels,
                model_name=self._config.gliner_model,
                threshold=self._config.gliner_threshold,
                cache_dir=self._config.model_cache_dir,
            )
            return result, (time.perf_counter() - t) * 1000

        # Intent-slot SLM is NOT invoked here — it runs earlier in
        # MemoryService.store_memory so its admission + state-change
        # heads can gate the ingest path.  Indexing only cares about
        # BM25 / SPLADE / GLiNER, which remain parallel.
        logger.info(
            "[store] Starting parallel indexing: BM25 + SPLADE + GLiNER",
        )
        bm25_ms, splade_ms, (auto_entities, extract_ms) = (
            await asyncio.gather(
                _do_bm25(), _do_splade(), _do_gliner(),
            )
        )
        logger.info(
            "[store] Parallel indexing complete: "
            "BM25=%.0fms SPLADE=%.0fms GLiNER=%.0fms",
            bm25_ms, splade_ms, extract_ms,
        )

        emit_stage("bm25_index", bm25_ms, memory_id=memory.id)
        if self._splade is not None:
            emit_stage("splade_index", splade_ms, memory_id=memory.id)

        # P1-temporal-experiment: split temporal spans out of the
        # GLiNER output before entity linking, resolve them to a
        # content range, and persist when non-empty.  The entity-
        # linking path below sees only entity-typed items.
        auto_entities = await self._persist_content_range(
            memory, auto_entities, emit_stage,
        )

        # Merge manual + auto-extracted entities (dedup by name)
        manual = list(entities_manual or [])
        manual_names = {e["name"].lower() for e in manual}
        all_entities = manual + [
            e for e in auto_entities
            if e["name"].lower() not in manual_names
        ]
        emit_stage("entity_extraction", extract_ms, {
            "extractor": "gliner",
            "auto_count": len(auto_entities),
            "manual_count": len(manual),
            "total_count": len(all_entities),
            "entity_names": [
                e["name"] for e in all_entities[:10]
            ],
        }, memory_id=memory.id)

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
        emit_stage("graph_linking", (time.perf_counter() - t0) * 1000, {
            "entities_linked": len(all_entities),
        }, memory_id=memory.id)

        # Co-occurrence edges
        if len(linked_entity_ids) > 1:
            self.build_cooccurrence_edges(
                memory.id, linked_entity_ids, emit_stage,
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
                spans.append(RawSpan(
                    text=str(item.get("name", "")),
                    label=label,
                    char_start=int(item.get("char_start", 0) or 0),
                    char_end=int(item.get("char_end", 0) or 0),
                ))
            else:
                entities_only.append(item)

        # Resolve relative expressions against the memory's own
        # observed_at if set — this lets historical replays encode
        # "yesterday" relative to the session date, not wall clock.
        ref = memory.observed_at or memory.created_at
        intervals = normalize_spans(spans, ref) if spans else []
        merged = merge_intervals(intervals)

        source, range_start, range_end = self._resolve_memory_range(
            merged, memory,
        )
        emit_stage("content_range_extracted", 0.0, {
            "span_count": len(spans),
            "resolved_intervals": len(intervals),
            "spans": [s.text for s in spans[:10]],
            "source": source,
            "range_start": range_start,
            "range_end": range_end,
        }, memory_id=memory.id)
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
                merged.end.isoformat(),    # type: ignore[attr-defined]
            )
        anchor = memory.observed_at or memory.created_at
        if anchor is None:
            return None, None, None
        # Day-wide interval anchored on the session timestamp.
        day_start = datetime(
            anchor.year, anchor.month, anchor.day,
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
        cooc_ids = linked_entity_ids[
            :self._config.cooccurrence_max_entities
        ]
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
        emit_stage(
            "cooccurrence_edges",
            (time.perf_counter() - t0) * 1000,
            {
                "edges_new": edges_new,
                "edges_incremented": edges_incremented,
                "entities_used": len(cooc_ids),
                "entities_capped": (
                    len(linked_entity_ids)
                    > self._config.cooccurrence_max_entities
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
        emit_stage("memory_node", 0.0, {
            "node_id": l1_node.id,
            "node_type": "atomic",
            "layer": "L1",
        }, memory_id=memory.id)

        # L2: Detect state change or declaration (or use caller subject)
        l2_node = await self._detect_and_create_l2_node(
            memory, content, all_entities, l1_node,
            admission_features, emit_stage,
            subject=subject,
        )

        # Reconcile entity state against existing states
        if (
            l2_node is not None
            and self._reconciliation is not None
            and self._config.temporal_enabled
            and l2_node.metadata.get("entity_id")
        ):
            await self._reconcile_entity_state(
                l2_node, memory.id, emit_stage,
            )

        # Episode formation (links to L1 atomic node)
        if self._episode is not None and self._config.temporal_enabled:
            await self._assign_episode(
                l1_node, memory, content, linked_entity_ids,
                emit_stage,
            )

    async def _detect_and_create_l2_node(
        self,
        memory: Memory,
        content: str,
        all_entities: list[dict],
        l1_node: MemoryNode,
        admission_features: object | None,
        emit_stage: Callable,
        subject: str | None = None,
    ) -> MemoryNode | None:
        """Detect entity state change and create L2 ENTITY_STATE node.

        When ``subject`` is provided (Option D' Part 4), create the
        node unconditionally with ``entity_id = subject`` and skip
        the state-change / regex / GLiNER-validation fork.
        """
        from ncms.domain.models import (
            EdgeType,
            GraphEdge,
            NodeType,
        )

        # ── Caller-asserted subject + SLM state_change gate ──────────
        # The subject kwarg alone is NOT enough to create an L2
        # ENTITY_STATE node.  An L2 node means "this memory IS a
        # state of entity X", but `subject` only asserts "this
        # memory is ABOUT entity X" — those are different things
        # for multi-section content (ADR sections, ticket threads,
        # patient-record paragraphs).
        #
        # The subject is already linked as a graph entity upstream
        # in memory_service.store_memory, which is all the TLG
        # vocabulary-cache rebuild needs (see vocabulary_cache.
        # _rebuild Signal 2).  We only create an L2 node here when
        # the SLM state_change head says declaration/retirement with
        # confidence — i.e. when the memory content genuinely marks
        # a state transition.
        #
        # NO REGEX on this path — the legacy regex state-declaration
        # fork below runs only when subject is NOT provided and the
        # SLM either abstained or is disabled.
        slm_label = (memory.structured or {}).get("intent_slot") or {}
        slm_state = slm_label.get("state_change")
        slm_state_conf = slm_label.get("state_change_confidence") or 0.0
        slm_state_change_confident = (
            slm_state in {"declaration", "retirement"}
            and slm_state_conf >= self._config.slm_confidence_threshold
        )
        if subject and slm_state_change_confident:
            # v7+ canonical state_value from role head (when present):
            # prefer the primary-role catalog canonical over the raw
            # content snippet so reconciliation has a stable
            # comparison key across memories.  Falls back to the
            # snippet when no role_spans (pre-v7 adapter or out-of-
            # catalog content).
            primary_span = None
            alt_span = None
            for r in slm_label.get("role_spans") or ():
                if r.get("role") == "primary" and primary_span is None:
                    primary_span = r
                elif r.get("role") == "alternative" and alt_span is None:
                    alt_span = r
            if primary_span:
                node_metadata = {
                    "entity_id": subject,
                    "state_key": primary_span["slot"],
                    "state_value": primary_span["canonical"],
                    "source": "caller_subject_slm_role_span",
                    "slm_state_change": slm_state,
                }
                if alt_span:
                    node_metadata["state_previous"] = alt_span["canonical"]
                    node_metadata["state_alternative"] = alt_span["canonical"]
            else:
                # Pin state_key="status" on the fallback since the
                # SLM topic head can misclassify domain-specific
                # content (a team-culture ADR observed as topic=
                # 'tooling' at conf 0.81).  Reconciliation lookup
                # stays consistent per subject.
                snippet = content.strip()[:200] or "(empty)"
                node_metadata = {
                    "entity_id": subject,
                    "state_key": "status",
                    "state_value": snippet,
                    "source": "caller_subject_slm_state_change",
                    "slm_state_change": slm_state,
                }
            l2_node = MemoryNode(
                memory_id=memory.id,
                node_type=NodeType.ENTITY_STATE,
                importance=memory.importance,
                metadata=node_metadata,
            )
            await self._store.save_memory_node(l2_node)
            await self._store.save_graph_edge(GraphEdge(
                source_id=l2_node.id,
                target_id=l1_node.id,
                edge_type=EdgeType.DERIVED_FROM,
                metadata={"layer": "L2_from_L1"},
            ))
            emit_stage("memory_node", 0.0, {
                "node_id": l2_node.id,
                "node_type": "entity_state",
                "layer": "L2",
                "derived_from": l1_node.id,
                "has_entity_state": True,
                "source": "caller_subject_slm_state_change",
            }, memory_id=memory.id)
            return l2_node
        if subject:
            # Subject provided but SLM did not declare a state change
            # — no L2 node.  Subject entity link (done upstream in
            # store_memory) is sufficient for vocabulary seeding.
            return None

        # SLM-first: when the intent-slot classifier is confident on
        # the state_change head, its prediction wins.  Falls through
        # to the regex path only when the SLM abstained or the flag
        # is off.  This replaces brittle YAML-frontmatter detection
        # (which false-positives on ADR templates) with the learned
        # classifier.
        slm_label = (memory.structured or {}).get("intent_slot") or {}
        slm_state = slm_label.get("state_change")
        slm_state_conf = slm_label.get("state_change_confidence") or 0.0
        slm_confident = (
            slm_state in {"declaration", "retirement"}
            and slm_state_conf
            >= self._config.slm_confidence_threshold
        )
        if slm_confident:
            _has_state_change = True
            _has_state_declaration = False  # SLM already decided
        else:
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
                or re.search(
                    r"(?:^|\n)##?\s*[Ss]tatus\s*[\n:]\s*\w+", content,
                )
                or re.search(
                    r"^\s*status\s*:\s*\w+",
                    content, re.MULTILINE | re.IGNORECASE,
                )
            )

        if not (_has_state_change or _has_state_declaration):
            return None

        # Pass the full SLM label dict through so the role-span path
        # in ``extract_entity_state_meta`` can source a canonical
        # state_value (the catalog's primary entry) instead of
        # falling through to the raw-sentence regex heuristics.
        node_metadata = self.extract_entity_state_meta(
            content, all_entities, slm_label=slm_label,
        )

        # Validate detected entity exists in GLiNER extraction set.
        # Skip this guard when the metadata came from the SLM role
        # span path — the primary-canonical form (``"postgresql"``,
        # ``"yugabytedb"``) is authoritative and may not appear in
        # the GLiNER entity names as-is (GLiNER often outputs
        # mixed-case variants like ``"PostgreSQL"``).
        if node_metadata.get("source") != "slm_role_span":
            _entity_names_lower = {
                e["name"].lower() for e in all_entities
            }
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
        emit_stage("memory_node", 0.0, {
            "node_id": l2_node.id,
            "node_type": "entity_state",
            "layer": "L2",
            "derived_from": l1_node.id,
            "has_entity_state": bool(
                node_metadata.get("entity_id"),
            ),
        }, memory_id=memory.id)

        return l2_node

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
                l2_node.id, exc_info=True,
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
                    "episode_id": (
                        episode_node.id if episode_node else None
                    ),
                    "action": "created" if episode_node else "none",
                },
                memory_id=memory.id,
            )

            if episode_node is not None:
                await self._episode.check_resolution_closure(
                    content, episode_node,
                )
        except Exception:
            logger.warning(
                "Episode formation failed for node %s, continuing",
                l1_node.id, exc_info=True,
            )
            emit_stage(
                "episode_formation_error",
                (time.perf_counter() - t0) * 1000,
                memory_id=memory.id,
            )

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
            candidate_ids = [
                mid for mid, _ in candidates if mid != memory.id
            ]
            candidate_ids = candidate_ids[
                :self._config.contradiction_candidate_limit
            ]

            # Also pull in graph-related memories via shared entities
            for e_data in all_entities[:5]:
                eid = self._graph.find_entity_by_name(e_data["name"])
                if eid:
                    related = self._graph.get_related_memory_ids(
                        [eid], depth=1,
                    )
                    for rid in related:
                        if (
                            rid != memory.id
                            and rid not in candidate_ids
                        ):
                            candidate_ids.append(rid)
                            if (
                                len(candidate_ids)
                                >= self._config.contradiction_candidate_limit
                            ):
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
                        memory, contradictions,
                    )
                    logger.info(
                        "Deferred contradiction check: %d "
                        "contradiction(s) for memory %s",
                        len(contradictions), memory.id,
                    )
        except Exception:
            logger.warning(
                "Deferred contradiction detection failed for memory %s",
                memory.id, exc_info=True,
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
                    "contradicted_by", [],
                )
                ex_contradictions.append({
                    "newer_memory_id": memory.id,
                    "contradiction_type": c["contradiction_type"],
                    "explanation": c["explanation"],
                    "severity": c["severity"],
                })
                ex_structured["contradicted_by"] = ex_contradictions
                existing.structured = ex_structured
                await self._store.update_memory(existing)
