"""Scoring pipeline: multi-signal candidate scoring and ranking.

Two-pass scoring:

1. ``_compute_raw_signals`` collects BM25, SPLADE, graph, ACT-R,
   recency, and temporal signals per candidate (plus reconciliation
   penalties).
2. ``_normalize_and_combine`` min-max normalizes each signal to
   ``[0, 1]`` and combines them via a weighted sum.

Per-query normalization fixes the fundamental scale mismatch where
SPLADE (5-200 range) previously dominated BM25 (1-15 range) despite
lower configured weights.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ncms.domain.intent import IntentResult, QueryIntent
from ncms.domain.models import EdgeType, ScoredMemory
from ncms.domain.scoring import (
    activation_noise,
    base_level_activation,
    conflict_annotation_penalty,
    graph_spreading_activation,
    hierarchy_match_bonus,
    intent_alignment_bonus,
    ppr_graph_score,
    recency_score,
    retrieval_probability,
    role_grounding_bonus,
    spreading_activation,
    supersession_penalty,
    total_activation,
)
from ncms.domain.temporal.parser import compute_temporal_proximity

if TYPE_CHECKING:
    from ncms.config import NCMSConfig
    from ncms.domain.protocols import GraphEngine, MemoryStore
    from ncms.infrastructure.observability.event_log import (
        EventLog,
        NullEventLog,
    )

logger = logging.getLogger(__name__)


class ScoringPipeline:
    """Multi-signal candidate scoring and ranking.

    Dependencies are injected via constructor — no hidden state is
    pulled from a larger service.  Each public method takes the data
    it needs explicitly, making the pipeline trivial to unit-test.
    """

    def __init__(
        self,
        store: MemoryStore,
        graph: GraphEngine,
        event_log: EventLog | NullEventLog,
        config: NCMSConfig,
    ) -> None:
        self._store = store
        self._graph = graph
        self._event_log = event_log
        self._config = config

    # ── Public API ───────────────────────────────────────────────────────

    async def score_and_rank(
        self,
        *,
        all_candidates: list[tuple[str, float]],
        bm25_scores: dict[str, float],
        splade_scores: dict[str, float],
        ce_scores: dict[str, float],
        context_entity_ids: list[str],
        nodes_by_memory: dict[str, list],
        intent_result: IntentResult | None,
        temporal_ref: object | None,
        domain: str | None,
        emit_stage: Callable,
        query_canonicals: set[str] | frozenset[str] | None = None,
    ) -> list[ScoredMemory]:
        """Score all candidates using multi-signal weighted combination.

        Phase H.3: ``query_canonicals`` carries the lowercased canonical
        forms of the query's extracted entities (surfaced from
        ``query_entity_names`` upstream in :class:`MemoryService.search`).
        Used by :func:`role_grounding_bonus` to reward memories where
        the query entity has ``role=primary`` in the SLM's per-span
        output.  ``None`` → role grounding is skipped (no signal).
        """
        # Load graph-derived context (association strengths, IDF, PPR)
        assoc_strengths, entity_idf, ppr_scores = (
            await self._load_graph_context(context_entity_ids)
        )

        # Resolve signal weights (globally or per-intent)
        w_bm25, w_actr, w_splade, w_graph, w_recency = (
            self._resolve_weights(intent_result)
        )

        # Batch preload memories + access times
        t0 = time.perf_counter()
        candidate_ids = [mid for mid, _ in all_candidates]
        memories_batch = await self._store.get_memories_batch(
            candidate_ids,
        )
        access_times_batch = (
            await self._store.get_access_times_batch(candidate_ids)
            if w_actr > 0 else {}
        )

        # Pass 1: collect raw signals
        raw_candidates = await self._compute_raw_signals(
            all_candidates=all_candidates,
            memories_batch=memories_batch,
            access_times_batch=access_times_batch,
            bm25_scores=bm25_scores,
            splade_scores=splade_scores,
            context_entity_ids=context_entity_ids,
            nodes_by_memory=nodes_by_memory,
            intent_result=intent_result,
            temporal_ref=temporal_ref,
            domain=domain,
            assoc_strengths=assoc_strengths,
            entity_idf=entity_idf,
            ppr_scores=ppr_scores,
            w_actr=w_actr,
            w_recency=w_recency,
            query_canonicals=query_canonicals,
        )

        # Pass 2: normalize and combine
        scored = self._normalize_and_combine(
            raw_candidates=raw_candidates,
            ce_scores=ce_scores,
            intent_result=intent_result,
            temporal_ref=temporal_ref,
            w_bm25=w_bm25, w_actr=w_actr, w_splade=w_splade,
            w_graph=w_graph, w_recency=w_recency,
        )

        emit_stage("actr_scoring", (time.perf_counter() - t0) * 1000, {
            "candidates_scored": len(raw_candidates),
            "passed_threshold": len(scored),
            "filtered_below_threshold": (
                len(raw_candidates) - len(scored)
            ),
            "top_activation": round(
                max(
                    (s.total_activation for s in scored),
                    default=0.0,
                ),
                3,
            ),
        })

        self._emit_debug_candidates(scored, intent_result)
        return scored

    async def _load_graph_context(
        self, context_entity_ids: list[str],
    ) -> tuple[
        dict[tuple[str, str], float] | None,
        dict[str, float] | None,
        dict[str, float],
    ]:
        """Load association strengths, IDF weights, and PPR scores.

        All three are optional query-time computations; each is
        guarded so a failure in one doesn't block the others.
        """
        assoc_strengths: dict[tuple[str, str], float] | None = None
        if self._config.dream_cycle_enabled:
            try:
                assoc_strengths = (
                    await self._store.get_association_strengths()
                )
                if not assoc_strengths:
                    assoc_strengths = None
            except Exception:
                logger.debug(
                    "Failed to load association strengths",
                    exc_info=True,
                )

        entity_idf: dict[str, float] | None = None
        if context_entity_ids:
            try:
                doc_freq = self._graph.get_entity_document_frequency()
                total_docs = max(self._graph.total_memory_count(), 1)
                entity_idf = {
                    eid: math.log(total_docs / df) if df > 0 else 0.0
                    for eid, df in doc_freq.items()
                }
            except Exception:
                logger.debug(
                    "Failed to compute entity IDF", exc_info=True,
                )

        ppr_scores: dict[str, float] = {}
        if context_entity_ids and self._config.scoring_weight_graph > 0:
            try:
                seed = {eid: 1.0 for eid in context_entity_ids}
                ppr_scores = self._graph.personalized_pagerank(seed)
                max_ppr = (
                    max(ppr_scores.values()) if ppr_scores else 0.0
                )
                if max_ppr > 0:
                    ppr_scores = {
                        k: v / max_ppr
                        for k, v in ppr_scores.items()
                    }
            except Exception:
                logger.debug(
                    "PPR failed, falling back to BFS", exc_info=True,
                )

        return assoc_strengths, entity_idf, ppr_scores

    def _resolve_weights(
        self, intent_result: IntentResult | None,
    ) -> tuple[float, float, float, float, float]:
        """Resolve ``(w_bm25, w_actr, w_splade, w_graph, w_recency)``.

        Uses global defaults unless intent-aware routing is enabled and
        a classified intent is present.
        """
        w_bm25 = self._config.scoring_weight_bm25
        w_actr = self._config.scoring_weight_actr
        w_splade = self._config.scoring_weight_splade
        w_graph = self._config.scoring_weight_graph
        w_recency = self._config.scoring_weight_recency
        if self._config.temporal_enabled and intent_result:
            with contextlib.suppress(Exception):
                w_bm25, w_splade, w_graph, w_recency = (
                    self._get_intent_weights(intent_result.intent)
                )
        return w_bm25, w_actr, w_splade, w_graph, w_recency

    def _emit_debug_candidates(
        self,
        scored: list[ScoredMemory],
        intent_result: IntentResult | None,
    ) -> None:
        """Emit per-candidate scoring diagnostics in pipeline-debug mode."""
        if not (self._config.pipeline_debug and scored):
            return
        intent_label = (
            intent_result.intent.value if intent_result else "unknown"
        )
        self._event_log.retrieval_debug(
            query="", intent=intent_label,
            candidates=[{
                "id": s.memory.id, "type": s.memory.type,
                "content": s.memory.content[:120],
                "bm25": round(s.bm25_score, 4),
                "splade": round(s.splade_score, 4),
                "graph": round(s.spreading, 4),
                "actr": round(s.total_activation, 4),
            } for s in sorted(
                scored, key=lambda x: x.total_activation,
                reverse=True,
            )[:20]],
            scores={}, agent_id=None,
        )

    # ── Pass 1: Raw Signals ──────────────────────────────────────────────

    async def _compute_raw_signals(
        self,
        *,
        all_candidates: list[tuple[str, float]],
        memories_batch: dict,
        access_times_batch: dict,
        bm25_scores: dict[str, float],
        splade_scores: dict[str, float],
        context_entity_ids: list[str],
        nodes_by_memory: dict[str, list],
        intent_result: IntentResult | None,
        temporal_ref: object | None,
        domain: str | None,
        assoc_strengths: dict | None,
        entity_idf: dict | None,
        ppr_scores: dict[str, float],
        w_actr: float,
        w_recency: float,
        query_canonicals: set[str] | frozenset[str] | None = None,
    ) -> list[dict]:
        """Pass 1: compute raw scoring signals for each candidate."""
        def _neighbor_fn(eid: str) -> list[tuple[str, float]]:
            return self._graph.get_neighbors_with_weights(eid)

        def _degree_fn(eid: str) -> int:
            return self._graph.get_entity_degree(eid)

        raw_candidates: list[dict] = []

        for memory_id, _ in all_candidates:
            memory = memories_batch.get(memory_id)
            if not memory:
                continue

            # Domain filter
            if domain and domain not in memory.domains and not any(
                d.startswith(domain) for d in memory.domains
            ):
                continue

            access_ages = access_times_batch.get(memory_id, [])
            bl = base_level_activation(
                access_ages, decay=self._config.actr_decay,
            )
            memory_entities = self._graph.get_entity_ids_for_memory(
                memory_id,
            )

            spread = 0.0
            if w_actr > 0:
                spread = spreading_activation(
                    memory_entity_ids=memory_entities,
                    context_entity_ids=context_entity_ids,
                    association_strengths=assoc_strengths,
                    source_activation=self._config.actr_max_spread,
                )

            if ppr_scores:
                graph_spread = ppr_graph_score(
                    memory_entity_ids=memory_entities,
                    ppr_scores=ppr_scores, entity_idf=entity_idf,
                )
            else:
                graph_spread = graph_spreading_activation(
                    memory_entity_ids=memory_entities,
                    context_entity_ids=context_entity_ids,
                    neighbor_fn=_neighbor_fn, entity_idf=entity_idf,
                    hop_decay=self._config.graph_hop_decay,
                    max_hops=self._config.graph_spreading_max_hops,
                    source_activation=self._config.actr_max_spread,
                    degree_fn=_degree_fn,
                )

            noise = activation_noise(sigma=self._config.actr_noise)
            nodes = nodes_by_memory.get(memory_id, [])
            node_types = [mn.node_type.value for mn in nodes]

            # Reconciliation penalties — intent-gated.  The supersession
            # / conflict penalties only make sense for queries that
            # ask about CURRENT STATE; they actively HURT historical /
            # general / event-reconstruction queries because the
            # superseded memory IS the gold answer for those.  Phase
            # G ablation B caught this: with v9 SLM producing more
            # state_change=declaration labels, more memories ended up
            # on supersession chains, and the indiscriminately-applied
            # penalty pushed the gold answer below its replacement.
            penalty, is_superseded, has_conflicts = (
                await self._compute_reconciliation_penalty(
                    nodes, intent_result=intent_result,
                )
            )

            # Hierarchy bonus
            h_bonus = 0.0
            if intent_result and node_types:
                h_bonus = hierarchy_match_bonus(
                    node_types, intent_result.target_node_types,
                    bonus=self._config.intent_hierarchy_bonus,
                )

            # Phase H.1 — per-memory intent-label × QueryIntent alignment.
            # The SLM's preference-intent label is exactly the kind of
            # query-side signal PATTERN_LOOKUP / STRATEGIC_REFLECTION
            # were waiting for: a memory tagged ``habitual`` IS the
            # answer to "what do you usually do?".  No-op when the
            # query intent has no alignment rule, when the memory has
            # no SLM label, or when the master temporal flag is off
            # (no QueryIntent → nothing to align against).
            ia_bonus = 0.0
            if (
                self._config.temporal_enabled
                and intent_result
                and self._config.scoring_weight_intent_alignment > 0
            ):
                aligned = self._INTENT_ALIGNMENT_TABLE.get(
                    intent_result.intent,
                )
                if aligned:
                    ia_bonus = intent_alignment_bonus(
                        memory_intent=self._memory_intent_label(memory),
                        aligned_intents=aligned,
                        bonus=self._config.intent_alignment_bonus,
                    )

            # Phase H.2 — state_change × QueryIntent alignment.  When
            # the query asks "what changed?", memories tagged with
            # state_change ∈ {declaration, retirement} are by
            # definition state-change events.  Reuses the
            # ``intent_alignment_bonus`` primitive: generic over
            # (label, aligned_set).  No-op when the query intent has
            # no state_change rule, when the memory has no SLM label,
            # or when the master temporal flag is off.
            sc_bonus = 0.0
            if (
                self._config.temporal_enabled
                and intent_result
                and self._config.scoring_weight_state_change_alignment > 0
            ):
                sc_aligned = self._STATE_CHANGE_ALIGNMENT_TABLE.get(
                    intent_result.intent,
                )
                if sc_aligned:
                    sc_bonus = intent_alignment_bonus(
                        memory_intent=(
                            self._memory_state_change_label(memory)
                        ),
                        aligned_intents=sc_aligned,
                        bonus=self._config.state_change_alignment_bonus,
                    )

            # Phase H.3 — role-grounding bonus.  When the query
            # mentions an entity, memories where that entity has
            # role=primary in the SLM's per-span output are genuinely
            # *about* the entity, not just string-matching it.  This
            # signal is independent of QueryIntent (applies to
            # FACT_LOOKUP, CURRENT_STATE_LOOKUP, all of them) so the
            # surface area is much larger than H.1.  No-op on
            # heuristic-fallback memories (empty role_spans) or
            # queries with no extracted entities — both correctly
            # return 0.0 from the primitive.
            rg_bonus = 0.0
            if (
                self._config.scoring_weight_role_grounding > 0
                and query_canonicals
            ):
                rg_bonus = role_grounding_bonus(
                    role_spans=self._memory_role_spans(memory),
                    query_canonicals=query_canonicals,
                    primary_bonus=self._config.role_grounding_bonus,
                )

            act = total_activation(
                bl, spread, noise, mismatch_penalty=0.0,
            )

            # Recency
            rec_score = 0.0
            if w_recency > 0 and memory.created_at:
                from datetime import UTC, datetime
                age_s = max(
                    0.0,
                    (datetime.now(UTC) - memory.created_at).total_seconds(),
                )
                rec_score = recency_score(
                    age_s,
                    half_life_days=self._config.recency_half_life_days,
                )

            # Temporal proximity against the event's true time.
            temporal_raw = 0.0
            if temporal_ref is not None:
                event_time = self._resolve_event_time(memory, nodes)
                if event_time is not None:
                    temporal_raw = compute_temporal_proximity(
                        event_time, temporal_ref,
                    )

            raw_candidates.append({
                "memory": memory, "memory_id": memory_id,
                "bm25_raw": bm25_scores.get(memory_id, 0.0),
                "splade_raw": splade_scores.get(memory_id, 0.0),
                "graph_raw": graph_spread, "temporal_raw": temporal_raw,
                "act": act, "bl": bl, "spread": spread, "noise": noise,
                "penalty": penalty, "h_bonus": h_bonus,
                "ia_bonus": ia_bonus,
                "sc_bonus": sc_bonus,
                "rg_bonus": rg_bonus,
                "rec_score": rec_score,
                "is_superseded": is_superseded,
                "has_conflicts": has_conflicts,
                "superseded_by": next(
                    (
                        mn.metadata.get("superseded_by")
                        for mn in nodes if not mn.is_current
                    ),
                    None,
                ),
                "node_types": node_types,
            })

        return raw_candidates

    @staticmethod
    def _resolve_event_time(
        memory: object,
        nodes: list,
    ) -> datetime | None:
        """Resolve the "when did this happen" timestamp for temporal scoring.

        Preference order:

        1. ``MemoryNode.observed_at`` on any loaded HTMG node (most
           specific — an L2 entity_state might have its own time).
        2. ``Memory.observed_at`` (bitemporal field set at ingest by the
           source, e.g. the session date for replayed conversations).
        3. ``Memory.created_at`` (fallback: NCMS ingest time).
        """
        for mn in nodes:
            if getattr(mn, "observed_at", None) is not None:
                return mn.observed_at
        if getattr(memory, "observed_at", None) is not None:
            return memory.observed_at  # type: ignore[attr-defined]
        return getattr(memory, "created_at", None)

    # Query intents for which a memory being on a supersession chain
    # is actually meaningful — the user is asking "what is X NOW",
    # so older states should rank below the current one.  For every
    # other intent class (fact lookup, historical / event reconstruction,
    # pattern / strategic, change detection) the OLDER memory IS often
    # the answer, so penalising it hurts retrieval.  See Phase G
    # ablation B + the docs/v9-mseb-slm-lift-findings.md writeup.
    _RECONCILIATION_PENALTY_INTENTS = frozenset({
        QueryIntent.CURRENT_STATE_LOOKUP,
    })

    # Phase H.1 — QueryIntent → set of per-memory intent labels that
    # are a clean semantic match.  The 5-head SLM emits one label per
    # memory drawn from ``INTENT_CATEGORIES`` (positive / negative /
    # habitual / difficulty / choice / none).  When the BM25 exemplar
    # classifier produces a QueryIntent with an entry in this table,
    # memories whose ``intent_slot.intent`` is in the aligned set get
    # a small additive bonus on top of the BM25 / SPLADE / graph mix.
    #
    # The table is conservative on purpose — only mappings where the
    # query→memory alignment is unambiguous earn an entry:
    #
    #   PATTERN_LOOKUP  ── "what's the recurring theme / what do you
    #                       usually do?" — ``habitual`` memories are
    #                       extracted as exactly that signal.
    #   STRATEGIC_REFLECTION ── "what should we do / what have we
    #                       learned?" — habits + explicit choices
    #                       both carry insight weight here.
    #
    # Other QueryIntents either care about state evolution (handled
    # by the reconciliation gate + temporal scoring) or have no
    # natural mapping to the preference taxonomy and are deliberately
    # absent.  Adding new mappings is one line each — see Phase H.5
    # (preference-style FACT_LOOKUP queries) for a likely follow-up.
    _INTENT_ALIGNMENT_TABLE: dict[QueryIntent, frozenset[str]] = {
        QueryIntent.PATTERN_LOOKUP: frozenset({"habitual"}),
        QueryIntent.STRATEGIC_REFLECTION: frozenset({"habitual", "choice"}),
    }

    # Phase H.2 — QueryIntent → set of state_change labels that align.
    # The 5-head SLM emits ``state_change`` per memory drawn from
    # ``STATE_CHANGES`` (declaration / retirement / none).  When the
    # query intent is CHANGE_DETECTION, memories tagged as actual
    # changes (declaration of a new state, retirement of an old one)
    # are by definition the answer.  Reuses the same scoring primitive
    # as H.1 (``intent_alignment_bonus``) — the primitive is generic
    # over (label, aligned_set), so passing the state_change field
    # works identically.  Conservative table: only CHANGE_DETECTION
    # has an alignment rule.  HISTORICAL_LOOKUP could plausibly align
    # with ``retirement`` but the older state itself (NOT the
    # retirement event) is usually the answer there, so deliberately
    # absent.  See Phase H.2 ablation.
    _STATE_CHANGE_ALIGNMENT_TABLE: dict[QueryIntent, frozenset[str]] = {
        QueryIntent.CHANGE_DETECTION: frozenset({"declaration", "retirement"}),
    }

    @staticmethod
    def _memory_intent_label(memory: object) -> str | None:
        """Pull the ``intent_slot.intent`` label baked into a Memory.

        Returns the string label or ``None`` when the memory lacks
        structured intent_slot output (heuristic fallback chain, or
        SLM disabled at ingest time).  Defensive against malformed
        ``structured`` payloads — bad shapes return ``None`` rather
        than raising into the scoring loop.
        """
        structured = getattr(memory, "structured", None)
        if not isinstance(structured, dict):
            return None
        slot = structured.get("intent_slot")
        if not isinstance(slot, dict):
            return None
        intent = slot.get("intent")
        return intent if isinstance(intent, str) and intent else None

    @staticmethod
    def _memory_state_change_label(memory: object) -> str | None:
        """Pull the ``intent_slot.state_change`` label baked into a Memory.

        Mirrors :meth:`_memory_intent_label` for the state_change
        head's output.  Returns the string label (declaration /
        retirement / none) or ``None`` on heuristic fallback or
        malformed payloads.
        """
        structured = getattr(memory, "structured", None)
        if not isinstance(structured, dict):
            return None
        slot = structured.get("intent_slot")
        if not isinstance(slot, dict):
            return None
        sc = slot.get("state_change")
        return sc if isinstance(sc, str) and sc else None

    @staticmethod
    def _memory_role_spans(memory: object) -> list[dict]:
        """Pull the ``intent_slot.role_spans`` list baked into a Memory.

        Phase H.3 — the 5-head SLM emits a role label per gazetteer-
        detected span (primary / alternative / casual / not_relevant).
        Memories that didn't go through the SLM (heuristic fallback
        chain, no adapter for the domain) return an empty list and
        the role-grounding bonus auto-noops on them.

        Returns ``[]`` (never ``None``) on any malformed payload —
        callers can iterate without nil-guards.
        """
        structured = getattr(memory, "structured", None)
        if not isinstance(structured, dict):
            return []
        slot = structured.get("intent_slot")
        if not isinstance(slot, dict):
            return []
        spans = slot.get("role_spans")
        if not isinstance(spans, list):
            return []
        return spans

    async def _compute_reconciliation_penalty(
        self,
        nodes: list,
        *,
        intent_result: IntentResult | None = None,
    ) -> tuple[float, bool, bool]:
        """Compute reconciliation penalties for superseded/conflicted states.

        Returns (penalty, is_superseded, has_conflicts).  The penalty
        is non-zero ONLY when the classified query intent is in
        :attr:`_RECONCILIATION_PENALTY_INTENTS` (currently just
        CURRENT_STATE_LOOKUP).  For every other intent class — and
        for queries we couldn't classify confidently — the
        is_superseded / has_conflicts flags are still returned so
        callers can include them in their output struct, but the
        scoring penalty stays at 0.0.
        """
        if not self._config.temporal_enabled or not nodes:
            return 0.0, False, False
        try:
            is_superseded = False
            has_conflicts = False
            for mn in nodes:
                if not mn.is_current:
                    is_superseded = True
                conflict_edges = await self._store.get_graph_edges(
                    mn.id, EdgeType.CONFLICTS_WITH,
                )
                if conflict_edges:
                    has_conflicts = True

            # Intent gate — only penalise when the query is
            # specifically asking for current state.
            apply_penalty = (
                intent_result is not None
                and intent_result.intent in self._RECONCILIATION_PENALTY_INTENTS
            )
            if not apply_penalty:
                return 0.0, is_superseded, has_conflicts

            sup_pen = supersession_penalty(
                is_superseded,
                self._config.reconciliation_supersession_penalty,
            )
            con_pen = conflict_annotation_penalty(
                has_conflicts,
                self._config.reconciliation_conflict_penalty,
            )
            return sup_pen + con_pen, is_superseded, has_conflicts
        except Exception:
            return 0.0, False, False

    # ── Pass 2: Normalize and Combine ────────────────────────────────────

    def _normalize_and_combine(
        self,
        *,
        raw_candidates: list[dict],
        ce_scores: dict[str, float],
        intent_result: IntentResult | None,
        temporal_ref: object | None,
        w_bm25: float, w_actr: float, w_splade: float,
        w_graph: float, w_recency: float,
    ) -> list[ScoredMemory]:
        """Pass 2: min-max normalize signals and compute combined scores."""
        if not raw_candidates:
            return []

        maxes = {
            "bm25": max(c["bm25_raw"] for c in raw_candidates) or 1.0,
            "splade": (
                max(c["splade_raw"] for c in raw_candidates) or 1.0
            ),
            "graph": max(c["graph_raw"] for c in raw_candidates) or 1.0,
            "temporal": (
                max(c["temporal_raw"] for c in raw_candidates) or 1.0
            ),
        }

        w_hierarchy = self._config.scoring_weight_hierarchy
        w_intent_align = self._config.scoring_weight_intent_alignment
        w_state_change_align = (
            self._config.scoring_weight_state_change_alignment
        )
        w_role_ground = self._config.scoring_weight_role_grounding
        w_temporal = (
            self._config.scoring_weight_temporal
            if temporal_ref is not None else 0.0
        )
        w_ce = self._config.scoring_weight_ce if ce_scores else 0.0
        actr_enabled = w_actr > 0

        min_ce, ce_range = self._compute_ce_range(ce_scores, raw_candidates)

        scored: list[ScoredMemory] = []
        for c in raw_candidates:
            sm = self._score_one_candidate(
                c=c, maxes=maxes, ce_scores=ce_scores,
                min_ce=min_ce, ce_range=ce_range,
                w_bm25=w_bm25, w_actr=w_actr, w_splade=w_splade,
                w_graph=w_graph, w_recency=w_recency,
                w_hierarchy=w_hierarchy,
                w_intent_align=w_intent_align,
                w_state_change_align=w_state_change_align,
                w_role_ground=w_role_ground,
                w_temporal=w_temporal,
                w_ce=w_ce, actr_enabled=actr_enabled,
                intent_result=intent_result,
            )
            if sm is not None:
                scored.append(sm)

        return scored

    @staticmethod
    def _compute_ce_range(
        ce_scores: dict[str, float],
        raw_candidates: list[dict],
    ) -> tuple[float, float]:
        """Min-max normalization constants for cross-encoder scores."""
        if not ce_scores:
            return 0.0, 1.0
        ce_vals = [
            ce_scores.get(c["memory_id"], 0.0) for c in raw_candidates
        ]
        min_ce = min(ce_vals) if ce_vals else 0.0
        ce_range = (
            (max(ce_vals) - min_ce)
            if ce_vals and max(ce_vals) > min_ce else 1.0
        )
        return min_ce, ce_range

    def _score_one_candidate(
        self,
        *,
        c: dict,
        maxes: dict[str, float],
        ce_scores: dict[str, float],
        min_ce: float,
        ce_range: float,
        w_bm25: float, w_actr: float, w_splade: float,
        w_graph: float, w_recency: float,
        w_hierarchy: float,
        w_intent_align: float,
        w_state_change_align: float,
        w_role_ground: float,
        w_temporal: float,
        w_ce: float, actr_enabled: bool,
        intent_result: IntentResult | None,
    ) -> ScoredMemory | None:
        """Normalize signals for one candidate and build ScoredMemory.

        Returns ``None`` when the candidate is filtered by the ACT-R
        threshold; otherwise returns the populated ``ScoredMemory``.
        """
        bm25_n = c["bm25_raw"] / maxes["bm25"]
        splade_n = c["splade_raw"] / maxes["splade"]
        graph_n = c["graph_raw"] / maxes["graph"]
        temporal_n = c["temporal_raw"] / maxes["temporal"]
        temporal_contrib = temporal_n * w_temporal

        # Phase H.1 / H.2 / H.3 — intent alignment + state_change
        # alignment + role grounding are additive on both score paths
        # (CE-rerank and weighted-sum), each gated by its own weight.
        # All three stay at 0.0 when their signal is absent (no
        # QueryIntent match, no SLM label on the memory, etc.) so
        # the lines below are a no-op cost when the SLM didn't tag
        # the memory.
        ia_contrib = c.get("ia_bonus", 0.0) * w_intent_align
        sc_contrib = c.get("sc_bonus", 0.0) * w_state_change_align
        rg_contrib = c.get("rg_bonus", 0.0) * w_role_ground

        if ce_scores:
            ce_norm = (
                (ce_scores.get(c["memory_id"], min_ce) - min_ce)
                / ce_range
            )
            combined = (
                ce_norm * w_ce
                + bm25_n * (1.0 - w_ce) * 0.67
                + splade_n * (1.0 - w_ce) * 0.33
                + temporal_contrib - c["penalty"]
                + ia_contrib + sc_contrib + rg_contrib
            )
        else:
            combined = (
                bm25_n * w_bm25 + c["act"] * w_actr
                + splade_n * w_splade + graph_n * w_graph
                + c["h_bonus"] * w_hierarchy
                + c["rec_score"] * w_recency
                + temporal_contrib - c["penalty"]
                + ia_contrib + sc_contrib + rg_contrib
            )

        # ACT-R threshold filter
        if actr_enabled:
            ret_prob = retrieval_probability(
                c["act"], threshold=self._config.actr_threshold,
                tau=self._config.actr_temperature,
            )
            if ret_prob < 0.05:
                return None
        else:
            ret_prob = 1.0

        return ScoredMemory(
            memory=c["memory"], bm25_score=c["bm25_raw"],
            splade_score=c["splade_raw"], base_level=c["bl"],
            spreading=c["graph_raw"], total_activation=combined,
            retrieval_prob=ret_prob,
            is_superseded=c["is_superseded"],
            has_conflicts=c["has_conflicts"],
            superseded_by=c["superseded_by"],
            node_types=c["node_types"],
            intent=(
                intent_result.intent.value if intent_result else None
            ),
            hierarchy_bonus=c["h_bonus"],
            temporal_score=temporal_contrib,
            # Phase H signal contributions (post-weight, the actual
            # additive impact on combined).  Surfaced for the
            # query_diagnostic event so operators can see which heads
            # moved each candidate's rank.
            intent_alignment_contrib=ia_contrib,
            state_change_alignment_contrib=sc_contrib,
            role_grounding_contrib=rg_contrib,
            # Phase G — penalty stored as POSITIVE (always >= 0); the
            # combined-score expression subtracts it, so reading
            # ``combined + reconciliation_penalty`` recovers the
            # pre-penalty score.  ``0.0`` when the intent gate
            # didn't fire OR no supersession/conflict edges existed.
            reconciliation_penalty=c["penalty"],
        )

    # ── Per-Intent Weight Routing ────────────────────────────────────────

    def _get_intent_weights(
        self, intent: QueryIntent,
    ) -> tuple[float, float, float, float]:
        """Resolve (w_bm25, w_splade, w_graph, w_recency) for the intent.

        Parsed from the config string for this intent.  Falls back to
        global defaults on parse error.
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
                raise ValueError(
                    f"Expected 4 weights, got {len(parts)}",
                )
            return (parts[0], parts[1], parts[2], parts[3])
        except (ValueError, TypeError):
            logger.warning(
                "Invalid intent weights for %s: %r", intent_key, raw,
            )
            return (
                self._config.scoring_weight_bm25,
                self._config.scoring_weight_splade,
                self._config.scoring_weight_graph,
                self._config.scoring_weight_recency,
            )
