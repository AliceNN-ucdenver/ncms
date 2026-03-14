"""Memory Service - orchestrates storage, indexing, graph, and scoring.

This is the primary entry point for memory operations:
store, search, recall, and manage the full retrieval pipeline.
"""

from __future__ import annotations

import logging
import time
import uuid

from ncms.config import NCMSConfig
from ncms.domain.entity_extraction import resolve_labels
from ncms.domain.intent import IntentResult, QueryIntent, classify_intent
from ncms.domain.models import (
    AccessRecord,
    Entity,
    Memory,
    Relationship,
    ScoredMemory,
    SearchLogEntry,
)
from ncms.domain.protocols import GraphEngine, IndexEngine, MemoryStore
from ncms.domain.scoring import (
    activation_noise,
    base_level_activation,
    conflict_annotation_penalty,
    hierarchy_match_bonus,
    retrieval_probability,
    spreading_activation,
    supersession_penalty,
    total_activation,
)
from ncms.infrastructure.observability.event_log import NullEventLog

logger = logging.getLogger(__name__)


class MemoryService:
    """Orchestrates the full memory lifecycle: store, index, search, score."""

    def __init__(
        self,
        store: MemoryStore,
        index: IndexEngine,
        graph: GraphEngine,
        config: NCMSConfig | None = None,
        event_log: object | None = None,
        splade: object | None = None,
        admission: object | None = None,
        reconciliation: object | None = None,
        episode: object | None = None,
        intent_classifier: object | None = None,
    ):
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config or NCMSConfig()
        # EventLog for dashboard observability (NullEventLog discards events silently)
        self._event_log = event_log or NullEventLog()
        # Optional SPLADE engine for sparse neural retrieval (duck-typed)
        self._splade = splade
        # Optional AdmissionService for Phase 1 admission scoring (duck-typed)
        self._admission = admission
        # Optional ReconciliationService for Phase 2 state reconciliation (duck-typed)
        self._reconciliation = reconciliation
        # Optional EpisodeService for Phase 3 episode formation (duck-typed)
        self._episode = episode
        # Optional BM25 exemplar intent classifier (Phase 4, duck-typed)
        self._intent_classifier = intent_classifier

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def graph(self) -> GraphEngine:
        return self._graph

    async def _get_cached_labels(self, domains: list[str]) -> dict[str, list[str]]:
        """Load domain-specific entity labels from consolidation_state."""
        import json as _json

        cached: dict[str, list[str]] = {}
        for domain in domains:
            raw = await self._store.get_consolidation_value(f"entity_labels:{domain}")
            if raw:
                try:
                    labels = _json.loads(raw)
                    if isinstance(labels, list):
                        cached[domain] = labels
                except Exception:
                    pass
        return cached

    # ── Entity State Extraction (Phase 2A) ───────────────────────────────

    @staticmethod
    def _extract_entity_state_meta(
        content: str, entities: list[dict],
    ) -> dict:
        """Extract entity state metadata from content and extracted entities.

        Heuristic: parse "entity: key = value" or "entity key is value" patterns
        from content. Falls back to using the first extracted entity as entity_id
        and the content as state_value with a generic state_key.

        Returns a dict suitable for MemoryNode.metadata with entity_id, state_key,
        state_value, and optionally state_scope.
        """
        import re

        # Try structured pattern: "EntityName: key = value"
        # e.g. "auth-service: status = deployed"
        pattern = re.compile(
            r"^([a-zA-Z0-9_\-]+)\s*:\s*([a-zA-Z0-9_\-]+)\s*=\s*(.+)$",
            re.MULTILINE,
        )
        match = pattern.search(content)
        if match:
            return {
                "entity_id": match.group(1).strip(),
                "state_key": match.group(2).strip(),
                "state_value": match.group(3).strip(),
            }

        # Try "EntityName key is/are/was/were value" pattern
        # e.g. "auth-service status is deployed"
        pattern2 = re.compile(
            r"^([a-zA-Z0-9_\-]+)\s+([a-zA-Z0-9_\-]+)\s+"
            r"(?:is|are|was|were|changed to|updated to|set to)\s+(.+)$",
            re.MULTILINE | re.IGNORECASE,
        )
        match2 = pattern2.search(content)
        if match2:
            return {
                "entity_id": match2.group(1).strip(),
                "state_key": match2.group(2).strip(),
                "state_value": match2.group(3).strip(),
            }

        # Fallback: use first entity as entity_id, content as value
        if entities:
            return {
                "entity_id": entities[0]["name"],
                "state_key": "state",
                "state_value": content[:500].strip(),
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

        # ── Admission scoring (Phase 1, optional) ────────────────────────
        admission_route: str | None = None
        if self._admission is not None and self._config.admission_enabled:
            t0 = time.perf_counter()
            try:
                from dataclasses import asdict as _asdict

                from ncms.domain.models import EphemeralEntry, MemoryNode, NodeType
                from ncms.domain.scoring import route_memory, score_admission

                features = await self._admission.compute_features(
                    content, domains=domains, source_agent=source_agent,
                )
                admission_score = score_admission(features)
                admission_route = route_memory(features, admission_score)

                feature_dict = _asdict(features)
                _emit_stage("admission", (time.perf_counter() - t0) * 1000, {
                    "score": round(admission_score, 3),
                    "route": admission_route,
                    "features": {k: round(v, 3) for k, v in feature_dict.items()},
                })
                self._event_log.admission_scored(
                    memory_id=None, score=admission_score, route=admission_route,
                    features=feature_dict, agent_id=source_agent,
                )

                if admission_route == "discard":
                    logger.info(
                        "Admission: discarding content (score=%.3f)", admission_score,
                    )
                    _emit_stage("complete", (time.perf_counter() - pipeline_start) * 1000, {
                        "result": "discarded", "admission_score": round(admission_score, 3),
                    })
                    # Return a Memory object but don't persist it
                    return Memory(
                        content=content, type=memory_type,
                        domains=domains or [], tags=tags or [],
                        source_agent=source_agent, project=project,
                        structured={"admission": {"score": admission_score, "route": "discard"}},
                    )

                if admission_route == "ephemeral_cache":
                    from datetime import UTC, datetime, timedelta

                    ttl = self._config.admission_ephemeral_ttl_seconds
                    now = datetime.now(UTC)
                    entry = EphemeralEntry(
                        content=content,
                        source_agent=source_agent,
                        domains=domains or [],
                        admission_score=admission_score,
                        ttl_seconds=ttl,
                        created_at=now,
                        expires_at=now + timedelta(seconds=ttl),
                    )
                    await self._store.save_ephemeral(entry)
                    logger.info(
                        "Admission: ephemeral cache (score=%.3f, ttl=%ds)",
                        admission_score, ttl,
                    )
                    _emit_stage("complete", (time.perf_counter() - pipeline_start) * 1000, {
                        "result": "ephemeral",
                        "admission_score": round(admission_score, 3),
                        "ephemeral_id": entry.id,
                    })
                    return Memory(
                        content=content, type=memory_type,
                        domains=domains or [], tags=tags or [],
                        source_agent=source_agent, project=project,
                        structured={
                            "admission": {
                                "score": admission_score,
                                "route": "ephemeral_cache",
                                "ephemeral_id": entry.id,
                            },
                        },
                    )

                # For atomic/entity_state/episode: attach features as structured metadata
                if structured is None:
                    structured = {}
                structured["admission"] = {
                    "score": round(admission_score, 3),
                    "route": admission_route,
                    **{k: round(v, 3) for k, v in feature_dict.items()},
                }

            except Exception:
                logger.warning(
                    "Admission scoring failed, proceeding without admission",
                    exc_info=True,
                )
                _emit_stage("admission_error", (time.perf_counter() - t0) * 1000)

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
        from ncms.infrastructure.extraction.gliner_extractor import extract_entities_gliner

        cached = await self._get_cached_labels(domains or [])
        labels = resolve_labels(domains or [], cached_labels=cached)
        auto_entities = extract_entities_gliner(
            content,
            model_name=self._config.gliner_model,
            threshold=self._config.gliner_threshold,
            labels=labels,
            cache_dir=self._config.model_cache_dir,
        )
        manual = list(entities or [])
        manual_names = {e["name"].lower() for e in manual}
        all_entities = manual + [e for e in auto_entities if e["name"].lower() not in manual_names]
        _emit_stage("entity_extraction", (time.perf_counter() - t0) * 1000, {
            "extractor": "gliner",
            "auto_count": len(auto_entities),
            "manual_count": len(manual),
            "total_count": len(all_entities),
            "entity_names": [e["name"] for e in all_entities[:10]],
        }, memory_id=memory.id)

        t0 = time.perf_counter()
        linked_entity_ids: list[str] = []  # Collect for episode formation
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

        # Write MemoryNode (Phase 1 — admission-routed, or Phase 3 — episodes enabled)
        _should_create_node = (
            admission_route in ("atomic_memory", "entity_state_update", "episode_fragment")
            or (self._config.episodes_enabled and self._episode is not None)
        )
        if _should_create_node:
            try:
                from ncms.domain.models import MemoryNode, NodeType

                node_type_map = {
                    "atomic_memory": NodeType.ATOMIC,
                    "entity_state_update": NodeType.ENTITY_STATE,
                    "episode_fragment": NodeType.ATOMIC,
                }

                # Build metadata for entity_state_update nodes (Phase 2A)
                node_metadata: dict = {}
                if admission_route == "entity_state_update":
                    node_metadata = self._extract_entity_state_meta(
                        content, all_entities,
                    )

                # Determine node type (default ATOMIC when no admission route)
                node_type = node_type_map.get(
                    admission_route or "", NodeType.ATOMIC,
                )

                node = MemoryNode(
                    memory_id=memory.id,
                    node_type=node_type,
                    importance=memory.importance,
                    metadata=node_metadata,
                )
                await self._store.save_memory_node(node)
                _emit_stage("memory_node", 0.0, {
                    "node_id": node.id,
                    "node_type": node.node_type.value,
                    "has_entity_state": bool(node_metadata.get("entity_id")),
                }, memory_id=memory.id)

                # Phase 2A: Reconcile entity state against existing states
                if (
                    admission_route == "entity_state_update"
                    and self._reconciliation is not None
                    and self._config.reconciliation_enabled
                    and node_metadata.get("entity_id")
                ):
                    t0_recon = time.perf_counter()
                    try:
                        results = await self._reconciliation.reconcile(node)  # type: ignore[attr-defined]
                        recon_data: dict = {
                            "node_id": node.id,
                            "results_count": len(results),
                            "relations": [
                                {"relation": r.relation, "existing": r.existing_node_id}
                                for r in results
                            ],
                        }
                        _emit_stage(
                            "reconciliation",
                            (time.perf_counter() - t0_recon) * 1000,
                            recon_data,
                            memory_id=memory.id,
                        )
                    except Exception:
                        logger.warning(
                            "Reconciliation failed for node %s, continuing",
                            node.id,
                            exc_info=True,
                        )
                        _emit_stage(
                            "reconciliation_error",
                            (time.perf_counter() - t0_recon) * 1000,
                            memory_id=memory.id,
                        )

                # Phase 3: Episode formation
                if (
                    self._episode is not None
                    and self._config.episodes_enabled
                ):
                    t0_ep = time.perf_counter()
                    try:
                        episode_node = await self._episode.assign_or_create(  # type: ignore[attr-defined]
                            fragment_node=node,
                            fragment_memory=memory,
                            entity_ids=linked_entity_ids,
                        )
                        ep_data: dict = {
                            "node_id": node.id,
                            "episode_id": (
                                episode_node.id if episode_node else None
                            ),
                            "action": (
                                "created" if episode_node else "none"
                            ),
                        }
                        _emit_stage(
                            "episode_formation",
                            (time.perf_counter() - t0_ep) * 1000,
                            ep_data,
                            memory_id=memory.id,
                        )

                        # Check for resolution closure
                        if episode_node is not None:
                            await self._episode.check_resolution_closure(  # type: ignore[attr-defined]
                                content, episode_node,
                            )
                    except Exception:
                        logger.warning(
                            "Episode formation failed for node %s, continuing",
                            node.id,
                            exc_info=True,
                        )
                        _emit_stage(
                            "episode_formation_error",
                            (time.perf_counter() - t0_ep) * 1000,
                            memory_id=memory.id,
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
        from ncms.infrastructure.extraction.gliner_extractor import extract_entities_gliner

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
                association_strengths=assoc_strengths,
                source_activation=self._config.actr_max_spread,
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
                        # Check for conflict edges
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

            act = total_activation(bl, spread, noise, mismatch_penalty=penalty)

            # Look up per-source scores
            bm25_score = bm25_scores.get(memory_id, 0.0)
            splade_score_val = splade_scores.get(memory_id, 0.0)

            # Combine BM25, SPLADE, activation, graph, and hierarchy scoring
            w_bm25 = self._config.scoring_weight_bm25
            w_actr = self._config.scoring_weight_actr
            w_splade = self._config.scoring_weight_splade
            w_graph = self._config.scoring_weight_graph
            w_hierarchy = self._config.scoring_weight_hierarchy
            combined = (
                bm25_score * w_bm25
                + act * w_actr
                + splade_score_val * w_splade
                + spread * w_graph  # Entity overlap signal (independent of ACT-R weight)
                + h_bonus * w_hierarchy
            )

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
                    is_superseded=mem_is_superseded,
                    has_conflicts=mem_has_conflicts,
                    superseded_by=mem_superseded_by,
                    node_types=candidate_node_types,
                    intent=intent_result.intent.value if intent_result else None,
                    hierarchy_bonus=h_bonus,
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

        results = scored[:limit]

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
        return await self._store.count_memories()

    def entity_count(self) -> int:
        return self._graph.entity_count()

    def relationship_count(self) -> int:
        return self._graph.relationship_count()
