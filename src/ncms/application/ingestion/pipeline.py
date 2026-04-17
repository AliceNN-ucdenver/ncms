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
from typing import TYPE_CHECKING, Any, cast

from ncms.application.label_cache import load_cached_labels
from ncms.domain.entity_extraction import resolve_labels
from ncms.domain.models import Memory, MemoryNode, Relationship

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

    # ── Entity State Extraction (shared with index_worker) ──────────────

    @staticmethod
    def extract_entity_state_meta(
        content: str, entities: list[dict],
    ) -> dict:
        """Extract entity state metadata from content + GLiNER entities.

        Heuristic patterns (tried in order):

        1. ``EntityName: key = value`` — structured assignment
        2. ``EntityName key changed/updated from X to Y`` — transition
        3. ``EntityName: key changed/updated from X to Y`` — colon + transition
        4. ``EntityName key is/was/set to value`` — declaration
        5. Markdown ``## Status\\n\\nvalue`` (ADRs, design docs)
        6. YAML ``status: value`` (checklists, config)
        7. Fallback: first GLiNER entity, first assignment-like line

        Returns a dict suitable for ``MemoryNode.metadata`` with
        ``entity_id``, ``state_key``, ``state_value``, and optionally
        ``state_previous`` or ``state_scope``.
        """
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
    ) -> Memory | tuple[str | None, object | None, dict | None]:
        """Run admission scoring.

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
            route = route_memory(features, score)

            feature_dict = _asdict(features)
            emit_stage("admission", (time.perf_counter() - t0) * 1000, {
                "score": round(score, 3), "route": route,
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

    async def run_inline_indexing(
        self,
        memory: Memory,
        content: str,
        domains: list[str] | None,
        entities_manual: list[dict] | None,
        emit_stage: Callable,
    ) -> tuple[list[dict], list[str]]:
        """Run BM25, SPLADE, GLiNER in parallel; link entities.

        Returns ``(all_entities, linked_entity_ids)``.
        """
        from ncms.infrastructure.extraction.gliner_extractor import (
            extract_entities_gliner,
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
            t = time.perf_counter()
            cached = await load_cached_labels(self._store, domains or [])
            gliner_labels = resolve_labels(
                domains or [], cached_labels=cached,
            )
            result = await asyncio.to_thread(
                extract_entities_gliner, content,
                model_name=self._config.gliner_model,
                threshold=self._config.gliner_threshold,
                labels=gliner_labels,
                cache_dir=self._config.model_cache_dir,
            )
            return result, (time.perf_counter() - t) * 1000

        logger.info(
            "[store] Starting parallel indexing: "
            "BM25 + SPLADE + GLiNER",
        )
        bm25_ms, splade_ms, (auto_entities, extract_ms) = (
            await asyncio.gather(_do_bm25(), _do_splade(), _do_gliner())
        )
        logger.info(
            "[store] Parallel indexing complete: "
            "BM25=%.0fms SPLADE=%.0fms GLiNER=%.0fms",
            bm25_ms, splade_ms, extract_ms,
        )

        emit_stage("bm25_index", bm25_ms, memory_id=memory.id)
        if self._splade is not None:
            emit_stage("splade_index", splade_ms, memory_id=memory.id)

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
    ) -> None:
        """Create HTMG nodes for a persisted memory.

        L1 ATOMIC node is always created.  L2 ENTITY_STATE node is
        additionally created if state change or declaration is
        detected.  Then reconcile against existing states and assign
        to an episode.
        """
        from ncms.domain.models import MemoryNode, NodeType

        # L1: always create atomic node
        l1_node = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ATOMIC,
            importance=memory.importance,
        )
        await self._store.save_memory_node(l1_node)
        emit_stage("memory_node", 0.0, {
            "node_id": l1_node.id,
            "node_type": "atomic",
            "layer": "L1",
        }, memory_id=memory.id)

        # L2: Detect state change or declaration
        l2_node = await self._detect_and_create_l2_node(
            memory, content, all_entities, l1_node,
            admission_features, emit_stage,
        )

        # Reconcile entity state against existing states
        if (
            l2_node is not None
            and self._reconciliation is not None
            and self._config.reconciliation_enabled
            and l2_node.metadata.get("entity_id")
        ):
            await self._reconcile_entity_state(
                l2_node, memory.id, emit_stage,
            )

        # Episode formation (links to L1 atomic node)
        if self._episode is not None and self._config.episodes_enabled:
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
    ) -> MemoryNode | None:
        """Detect entity state change and create L2 ENTITY_STATE node."""
        from ncms.domain.models import (
            EdgeType,
            GraphEdge,
            NodeType,
        )

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

        node_metadata = self.extract_entity_state_meta(
            content, all_entities,
        )

        # Validate detected entity exists in GLiNER extraction set
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
