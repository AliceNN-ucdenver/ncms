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

        Strategy chain — first match wins:

        1. **SLM role-span path** (v7+ preferred) — when the SLM's
           role_head emitted a primary-role span, use the canonical
           form as ``state_value`` and the slot as ``state_key``.
           Reconciliation gets a stable comparison key.
        2. **Regex pattern chain** (cold-start fallback when SLM
           absent) — six patterns in order: assignment / transition /
           colon-transition / declaration / markdown-status /
           yaml-status.
        3. **First-line fallback** — when no pattern matches but
           entities are present, attach the first assignment-like
           line as the state value.

        Returns ``{}`` when nothing matches (no entities, no patterns
        — caller skips L2 creation).  Otherwise returns a dict suitable
        for ``MemoryNode.metadata`` with ``entity_id``, ``state_key``,
        ``state_value``, and optionally ``state_previous``,
        ``state_alternative``, ``source``.
        """
        meta = IngestionPipeline._meta_from_role_spans(slm_label, entities)
        if meta is not None:
            return meta
        for pattern_fn in IngestionPipeline._STATE_PATTERN_FNS:
            meta = pattern_fn(content, entities)
            if meta is not None:
                return meta
        return IngestionPipeline._meta_first_line_fallback(
            content, entities,
        )

    @staticmethod
    def _meta_from_role_spans(
        slm_label: dict | None, entities: list[dict],
    ) -> dict | None:
        """Strategy 1 — v7+ SLM role-span path (preferred).

        Subject-resolution: prefer a GLiNER entity that ISN'T the
        primary or alternative canonical, so the state belongs to
        something other than the value that changed.  Example:
        "auth-service migrated from PostgreSQL to CockroachDB" →
        GLiNER finds {auth-service, PostgreSQL, CockroachDB};
        primary=CockroachDB, alt=PostgreSQL, so subject =
        auth-service.
        """
        if not (slm_label and slm_label.get("role_spans")):
            return None
        role_spans = slm_label["role_spans"]
        primary = next(
            (r for r in role_spans if r.get("role") == "primary"),
            None,
        )
        if not primary:
            return None
        alt = next(
            (r for r in role_spans if r.get("role") == "alternative"),
            None,
        )
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

    # Compiled patterns — module-level for reuse across calls.
    _RX_ASSIGN = re.compile(
        r"^([a-zA-Z0-9_\-]+)\s*:\s*([a-zA-Z0-9_\-]+)\s*=\s*(.+)$",
        re.MULTILINE,
    )
    _RX_TRANSITION = re.compile(
        r"^([a-zA-Z0-9_\-]+)\s+([a-zA-Z0-9_\-]+)\s+"
        r"(?:changed|updated)\s+from\s+(.+?)\s+to\s+(.+?)"
        r"(?:\s+(?:due|for|after|because)\b.*)?$",
        re.MULTILINE | re.IGNORECASE,
    )
    _RX_COLON_TRANSITION = re.compile(
        r"^([a-zA-Z0-9_\-]+)\s*:\s*([a-zA-Z0-9_\-]+)\s+"
        r"(?:changed|updated)\s+from\s+(.+?)\s+to\s+(.+?)"
        r"(?:\s+(?:due|for|after|because|per)\b.*)?$",
        re.MULTILINE | re.IGNORECASE,
    )
    _RX_DECLARATION = re.compile(
        r"^([a-zA-Z0-9_\-]+)\s+([a-zA-Z0-9_\-]+)\s+"
        r"(?:is|are|was|were|changed to|updated to|set to)\s+(.+)$",
        re.MULTILINE | re.IGNORECASE,
    )
    _RX_MD_STATUS = re.compile(
        r"^#\s+(.+?)$.*?^##?\s*[Ss]tatus\s*$\s*^(\w[\w\s]*)$",
        re.MULTILINE | re.DOTALL,
    )
    _RX_YAML_STATUS = re.compile(
        r"^\s*status\s*:\s*(\w[\w_\-]*)",
        re.MULTILINE | re.IGNORECASE,
    )

    @staticmethod
    def _meta_assign(content: str, _entities: list[dict]) -> dict | None:
        """Pattern 1 — ``EntityName: key = value``."""
        m = IngestionPipeline._RX_ASSIGN.search(content)
        if not m:
            return None
        return {
            "entity_id": m.group(1).strip(),
            "state_key": m.group(2).strip(),
            "state_value": m.group(3).strip(),
        }

    @staticmethod
    def _meta_transition(
        content: str, _entities: list[dict],
    ) -> dict | None:
        """Pattern 2 — ``Entity key changed/updated from X to Y``."""
        m = IngestionPipeline._RX_TRANSITION.search(content)
        if not m:
            return None
        return {
            "entity_id": m.group(1).strip(),
            "state_key": m.group(2).strip(),
            "state_value": m.group(4).strip(),
            "state_previous": m.group(3).strip(),
        }

    @staticmethod
    def _meta_colon_transition(
        content: str, _entities: list[dict],
    ) -> dict | None:
        """Pattern 3 — ``Entity: key changed/updated from X to Y``."""
        m = IngestionPipeline._RX_COLON_TRANSITION.search(content)
        if not m:
            return None
        return {
            "entity_id": m.group(1).strip(),
            "state_key": m.group(2).strip(),
            "state_value": m.group(4).strip(),
            "state_previous": m.group(3).strip(),
        }

    @staticmethod
    def _meta_declaration(
        content: str, _entities: list[dict],
    ) -> dict | None:
        """Pattern 4 — ``Entity key is/was/set to value``."""
        m = IngestionPipeline._RX_DECLARATION.search(content)
        if not m:
            return None
        return {
            "entity_id": m.group(1).strip(),
            "state_key": m.group(2).strip(),
            "state_value": m.group(3).strip(),
        }

    @staticmethod
    def _meta_md_status(
        content: str, entities: list[dict],
    ) -> dict | None:
        """Pattern 5 — Markdown ``## Status\\n\\nvalue`` (ADRs)."""
        m = IngestionPipeline._RX_MD_STATUS.search(content)
        if not (m and entities):
            return None
        title_lower = m.group(1).strip().lower()
        status_val = m.group(2).strip()
        entity_id = entities[0]["name"]
        for ent in entities:
            if ent["name"].lower() in title_lower:
                entity_id = ent["name"]
                break
        return {
            "entity_id": entity_id,
            "state_key": "status",
            "state_value": status_val,
        }

    @staticmethod
    def _meta_yaml_status(
        content: str, entities: list[dict],
    ) -> dict | None:
        """Pattern 6 — YAML ``status: value``."""
        m = IngestionPipeline._RX_YAML_STATUS.search(content)
        if not (m and entities):
            return None
        return {
            "entity_id": entities[0]["name"],
            "state_key": "status",
            "state_value": m.group(1).strip(),
        }

    @staticmethod
    def _meta_first_line_fallback(
        content: str, entities: list[dict],
    ) -> dict:
        """Final fallback — first entity + first assignment-like line.

        Returns ``{}`` when no entities (caller skips L2 creation).
        """
        if not entities:
            return {}
        best_line = ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and any(c in stripped for c in ("=", ":", "→")):
                best_line = stripped[:200]
                break
        if not best_line:
            best_line = (
                content.splitlines()[0].strip()[:200] if content else ""
            )
        return {
            "entity_id": entities[0]["name"],
            "state_key": "state",
            "state_value": best_line,
        }

    # Strategy chain consumed by ``extract_entity_state_meta`` —
    # first match wins.  Defined as a class attribute (after the
    # method definitions resolve) so the chain is allocated once
    # per process, not per call.  Order matters: more-specific
    # patterns (assign / transition) before less-specific (yaml /
    # markdown).
    _STATE_PATTERN_FNS: tuple = (
        _meta_assign,
        _meta_transition,
        _meta_colon_transition,
        _meta_declaration,
        _meta_md_status,
        _meta_yaml_status,
    )

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
                content, domains=domains, source_agent=source_agent,
            )
            score = score_admission(features)
            route, slm_route = self._resolve_admission_route(
                intent_slot_label, features, score,
            )
            feature_dict = _asdict(features)
            self._emit_admission_event(
                t0=t0, score=score, route=route, slm_route=slm_route,
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
                    features.state_change_signal, importance,
                )
                return self._build_persist_continuation(
                    structured, score, "persist", feature_dict, features,
                )

            if route == "discard":
                return self._handle_discard_route(
                    content=content, memory_type=memory_type,
                    domains=domains, tags=tags,
                    source_agent=source_agent, project=project,
                    score=score,
                    emit_stage=emit_stage,
                    pipeline_start=pipeline_start,
                )
            if route == "ephemeral_cache":
                return await self._handle_ephemeral_route(
                    content=content, memory_type=memory_type,
                    domains=domains, tags=tags,
                    source_agent=source_agent, project=project,
                    score=score,
                    emit_stage=emit_stage,
                    pipeline_start=pipeline_start,
                )
            return self._build_persist_continuation(
                structured, score, route, feature_dict, features,
            )

        except Exception:
            logger.warning(
                "Admission scoring failed, proceeding without admission",
                exc_info=True,
            )
            emit_stage(
                "admission_error", (time.perf_counter() - t0) * 1000,
            )
            return None, None, structured

    def _resolve_admission_route(
        self,
        intent_slot_label: Any | None,
        features: object,
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
            "admission", (time.perf_counter() - t0) * 1000, {
                "score": round(score, 3), "route": route,
                "route_source": (
                    "intent_slot" if slm_route is not None else "regex"
                ),
                "features": {
                    k: round(v, 3) for k, v in feature_dict.items()
                },
            },
        )
        self._event_log.admission_scored(
            memory_id=None, score=score, route=route,
            features=feature_dict, agent_id=source_agent,
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
            "score": round(score, 3), "route": route,
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
        slot_entities_present: bool = False,
    ) -> tuple[list[dict], list[str]]:
        """Run BM25, SPLADE, GLiNER in parallel; link entities.

        When ``slot_entities_present`` is True, the SLM slot head already
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
            if slot_entities_present:
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

        # CTLG v8+: extract causal edges from memory-voice cue tags.
        # No-op for v7.x adapters (cue_tags empty).  Gated on
        # temporal_enabled — same flag that governs reconciliation.
        if self._config.temporal_enabled:
            await self._extract_and_persist_causal_edges(
                memory, l1_node, l2_node, emit_stage,
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

        Two strategies dispatched by the ``subject`` kwarg:

        - **Caller-asserted subject** (Option D' Part 4) — when the
          caller knows the entity-subject of this memory (MSEB
          backend, ticket system, patient record), create the L2
          node only when the SLM state_change head says declaration
          / retirement with confidence.  Subject alone is NOT enough:
          it asserts "this memory is ABOUT X" but L2 means "this
          memory IS a state of X".
        - **No subject** — SLM-first state-change detection via the
          shared ``slm_state_change_decision`` helper, with regex
          fallback only when the LoRA adapter didn't run.
        """
        slm_label = (memory.structured or {}).get("intent_slot") or {}
        if subject:
            return await self._create_l2_with_subject(
                memory=memory, content=content, l1_node=l1_node,
                slm_label=slm_label, subject=subject,
                emit_stage=emit_stage,
            )
        return await self._create_l2_via_state_detection(
            memory=memory, content=content,
            all_entities=all_entities, l1_node=l1_node,
            admission_features=admission_features,
            slm_label=slm_label, emit_stage=emit_stage,
        )

    async def _create_l2_with_subject(
        self,
        *,
        memory: Memory,
        content: str,
        l1_node: MemoryNode,
        slm_label: dict,
        subject: str,
        emit_stage: Callable,
    ) -> MemoryNode | None:
        """L2 path 1 — caller-asserted subject + SLM-confident state change.

        Creates the L2 node ONLY when the SLM state_change head
        emitted ``declaration`` or ``retirement`` above the confidence
        floor.  Subject alone is insufficient (would create false-
        positive L2s on multi-section ADRs / patient threads).
        """
        slm_state = slm_label.get("state_change")
        slm_state_conf = slm_label.get("state_change_confidence") or 0.0
        slm_change_confident = (
            slm_state in {"declaration", "retirement"}
            and slm_state_conf >= self._config.slm_confidence_threshold
        )
        if not slm_change_confident:
            return None

        node_metadata = self._build_subject_l2_metadata(
            content=content, slm_label=slm_label,
            slm_state=slm_state, subject=subject,
        )
        return await self._save_l2_node(
            memory=memory, l1_node=l1_node,
            node_metadata=node_metadata,
            emit_stage=emit_stage,
            extra_event_fields={
                "has_entity_state": True,
                "source": "caller_subject_slm_state_change",
            },
        )

    @staticmethod
    def _build_subject_l2_metadata(
        *,
        content: str,
        slm_label: dict,
        slm_state: str,
        subject: str,
    ) -> dict:
        """Build the L2 node metadata for the caller-subject path.

        Prefers the v7+ role_head's canonical primary span (e.g.
        ``database=postgresql``) over the raw content snippet, so
        reconciliation has a stable comparison key.  Falls back to
        ``state_key="status"`` + content snippet on pre-v7 adapters
        or out-of-catalog content.
        """
        primary_span = None
        alt_span = None
        for r in slm_label.get("role_spans") or ():
            role = r.get("role")
            if role == "primary" and primary_span is None:
                primary_span = r
            elif role == "alternative" and alt_span is None:
                alt_span = r
        if primary_span:
            meta = {
                "entity_id": subject,
                "state_key": primary_span["slot"],
                "state_value": primary_span["canonical"],
                "source": "caller_subject_slm_role_span",
                "slm_state_change": slm_state,
            }
            if alt_span:
                meta["state_previous"] = alt_span["canonical"]
                meta["state_alternative"] = alt_span["canonical"]
            return meta
        # Fallback — pin state_key="status" because the SLM topic
        # head can misclassify domain-specific content; reconciliation
        # lookup needs a consistent key per subject.
        snippet = content.strip()[:200] or "(empty)"
        return {
            "entity_id": subject,
            "state_key": "status",
            "state_value": snippet,
            "source": "caller_subject_slm_state_change",
            "slm_state_change": slm_state,
        }

    async def _create_l2_via_state_detection(
        self,
        *,
        memory: Memory,
        content: str,
        all_entities: list[dict],
        l1_node: MemoryNode,
        admission_features: object | None,
        slm_label: dict,
        emit_stage: Callable,
    ) -> MemoryNode | None:
        """L2 path 2 — SLM-first state detection (cold-start regex fallback).

        The Phase I.2 ``slm_state_change_decision`` helper is the
        primary path: when the LoRA adapter ran confidently, its
        verdict (incl. ``"none"``) is authoritative.  The regex
        block fires only on cold-start deployments without an
        adapter loaded.
        """
        if not self._state_change_detected(
            slm_label=slm_label, content=content,
            admission_features=admission_features,
        ):
            return None

        # Pass the full SLM label dict through so role_spans drive
        # extract_entity_state_meta to source canonical state_values.
        node_metadata = self.extract_entity_state_meta(
            content, all_entities, slm_label=slm_label,
        )
        if not node_metadata:
            return None

        # Validate detected entity exists in GLiNER set, EXCEPT
        # when metadata came from the SLM role span (canonical form
        # may not match GLiNER's mixed-case variant verbatim).
        if node_metadata.get("source") != "slm_role_span":
            entity_names_lower = {
                e["name"].lower() for e in all_entities
            }
            detected = node_metadata.get("entity_id", "")
            if detected.lower() not in entity_names_lower:
                return None

        return await self._save_l2_node(
            memory=memory, l1_node=l1_node,
            node_metadata=node_metadata,
            emit_stage=emit_stage,
            extra_event_fields={
                "has_entity_state": bool(node_metadata.get("entity_id")),
            },
        )

    def _state_change_detected(
        self,
        *,
        slm_label: dict,
        content: str,
        admission_features: object | None,
    ) -> bool:
        """Decide whether the L2-creation path should run.

        SLM-first via the shared decision helper; falls through to
        regex only when the LoRA didn't run.  Returns True iff there's
        a detected state-change event OR a state-declaration pattern.
        """
        from ncms.domain.intent_slot_taxonomy import (
            slm_state_change_decision,
        )

        slm_decision = slm_state_change_decision(
            slm_label,
            threshold=self._config.slm_confidence_threshold,
        )
        if slm_decision is not None:
            has_state_change, has_state_declaration = slm_decision
            return has_state_change or has_state_declaration

        # Cold-start regex/heuristic fallback.
        has_state_change = (
            admission_features is not None
            and hasattr(admission_features, "state_change_signal")
            and admission_features.state_change_signal >= 0.35
        )
        has_state_declaration = bool(
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
        return has_state_change or has_state_declaration

    async def _save_l2_node(
        self,
        *,
        memory: Memory,
        l1_node: MemoryNode,
        node_metadata: dict,
        emit_stage: Callable,
        extra_event_fields: dict,
    ) -> MemoryNode:
        """Persist an L2 ENTITY_STATE node + DERIVED_FROM edge to L1.

        Shared tail of both L2-creation strategies.  Emits the
        ``memory_node`` pipeline event with the ``extra_event_fields``
        merged in (each strategy carries its own provenance metadata).
        """
        from ncms.domain.models import EdgeType, GraphEdge, NodeType

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
        event_fields = {
            "node_id": l2_node.id,
            "node_type": "entity_state",
            "layer": "L2",
            "derived_from": l1_node.id,
        }
        event_fields.update(extra_event_fields)
        emit_stage(
            "memory_node", 0.0, event_fields, memory_id=memory.id,
        )
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

    # ── Stage 6.5: CTLG causal-edge extraction (v8+) ───────────────────

    async def _extract_and_persist_causal_edges(
        self,
        memory: Memory,
        l1_node: MemoryNode,
        l2_node: MemoryNode | None,
        emit_stage: Callable,
    ) -> None:
        """Extract CAUSED_BY / ENABLES edges from memory-voice cue tags.

        Gated on ``cue_tags`` presence in
        ``memory.structured["intent_slot"]``.  No-op on pre-CTLG
        adapters (v9 ships ``cue: 0``).  When the dedicated CTLG
        sibling adapter ships (see ``docs/research/ctlg-design.md``)
        cue tags will populate, this method will turn them into
        typed graph edges, and the dispatcher can walk causal chains
        directly.

        Best-effort: any lookup error is logged and swallowed; a
        dropped pair doesn't break ingest.
        """
        tokens = self._parse_cue_tags(memory)
        if not tokens:
            return

        t0 = time.perf_counter()
        pairs = self._extract_causal_pairs_safe(tokens, memory)
        if not pairs:
            return

        lookup = await self._build_causal_surface_lookup(
            memory=memory, l2_node=l2_node, pairs=pairs,
        )
        emitted, total_edges = await self._persist_causal_edges(
            pairs=pairs, lookup=lookup, memory=memory,
        )
        if emitted > 0:
            emit_stage(
                "ctlg_causal_edges",
                (time.perf_counter() - t0) * 1000,
                {
                    "memory_id": memory.id,
                    "n_pairs_extracted": len(pairs),
                    "n_edges_persisted": emitted,
                    "n_pairs_unresolved": len(pairs) - total_edges,
                },
                memory_id=memory.id,
            )

    @staticmethod
    def _parse_cue_tags(memory: Memory) -> list:
        """Deserialise ``intent_slot.cue_tags`` into TaggedToken dataclasses.

        Returns ``[]`` when no cues present (pre-CTLG adapter or
        empty cue head) or when every entry is malformed.
        """
        from ncms.domain.tlg.cue_taxonomy import TaggedToken

        slm_label = (memory.structured or {}).get("intent_slot") or {}
        cue_tag_dicts = slm_label.get("cue_tags") or []
        if not cue_tag_dicts:
            return []
        tokens: list[TaggedToken] = []
        for t in cue_tag_dicts:
            try:
                tokens.append(TaggedToken(
                    char_start=int(t["char_start"]),
                    char_end=int(t["char_end"]),
                    surface=str(t["surface"]),
                    cue_label=t["cue_label"],
                    confidence=float(t.get("confidence", 1.0)),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return tokens

    def _extract_causal_pairs_safe(
        self, tokens: list, memory: Memory,
    ) -> list:
        """Wrap the causal-extractor in a try/except.

        Returns ``[]`` when the extractor raises; logs the error
        but doesn't break ingest.
        """
        from ncms.domain.tlg.causal_extractor import extract_causal_pairs

        try:
            return extract_causal_pairs(
                tokens,
                min_confidence=getattr(
                    self._config, "ctlg_causal_min_confidence", 0.6,
                ),
            )
        except Exception:
            logger.warning(
                "[ctlg] causal extraction failed for memory %s",
                memory.id, exc_info=True,
            )
            return []

    async def _build_causal_surface_lookup(
        self,
        *,
        memory: Memory,
        l2_node: MemoryNode | None,
        pairs: list,
    ) -> dict[str, str]:
        """Resolve cue surfaces → memory IDs.

        Two-pass: (1) this memory's L2 node values + entity_id;
        (2) entity-state nodes elsewhere in the graph for any
        surface still unresolved.  Surfaces that don't resolve get
        dropped from the resulting edge set (dangling-edge guard).
        """
        lookup: dict[str, str] = {}
        if l2_node is not None:
            sv = str(
                l2_node.metadata.get("state_value", ""),
            ).lower().strip()
            eid = str(
                l2_node.metadata.get("entity_id", ""),
            ).lower().strip()
            if sv:
                lookup[sv] = memory.id
            if eid and eid not in lookup:
                lookup[eid] = memory.id

        needed: set[str] = set()
        for p in pairs:
            for surf in (p.effect_surface, p.cause_surface):
                surf_low = surf.lower()
                if surf_low not in lookup:
                    needed.add(surf_low)

        if needed:
            try:
                l2_candidates = (
                    await self._store.get_memory_nodes_by_type(
                        "entity_state",
                    )
                )
                for node in l2_candidates:
                    sv = str(
                        node.metadata.get("state_value", ""),
                    ).lower().strip()
                    eid = str(
                        node.metadata.get("entity_id", ""),
                    ).lower().strip()
                    if sv and sv in needed and sv not in lookup:
                        lookup[sv] = node.memory_id
                    if eid and eid in needed and eid not in lookup:
                        lookup[eid] = node.memory_id
            except Exception:
                logger.warning(
                    "[ctlg] entity_state lookup failed — some pairs "
                    "may be dropped", exc_info=True,
                )
        return lookup

    async def _persist_causal_edges(
        self,
        *,
        pairs: list,
        lookup: dict[str, str],
        memory: Memory,
    ) -> tuple[int, int]:
        """Persist resolved causal pairs as typed GraphEdges.

        Returns ``(n_emitted, n_total_edges)`` — n_total_edges
        is what the resolver produced (≤ len(pairs)); n_emitted
        is what survived the per-edge save.  Direction convention:
        causal edges go effect → cause (src=effect, dst=cause).
        """
        from ncms.domain.models import EdgeType, GraphEdge
        from ncms.domain.tlg.causal_extractor import pairs_to_causal_edges

        edges = pairs_to_causal_edges(
            pairs, surface_to_memory_id=lookup,
        )
        emitted = 0
        for edge in edges:
            edge_type = (
                EdgeType.CAUSED_BY if edge.edge_type == "caused_by"
                else EdgeType.ENABLES
            )
            try:
                await self._store.save_graph_edge(GraphEdge(
                    source_id=edge.src,
                    target_id=edge.dst,
                    edge_type=edge_type,
                    metadata={
                        "cue_type": edge.cue_type,
                        "source": "ctlg_cue_head",
                        "confidence": round(edge.confidence, 3),
                    },
                ))
                emitted += 1
            except Exception:
                logger.warning(
                    "[ctlg] failed to persist causal edge %s -> %s",
                    edge.src, edge.dst, exc_info=True,
                )
        return emitted, len(edges)

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
