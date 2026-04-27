"""Retrieval pipeline: candidate discovery, reranking, and expansion.

Three stages — each a public method on ``RetrievalPipeline``:

1. **retrieve_candidates** — Parallel BM25 + SPLADE + configured query
   entity extraction, fused via Reciprocal Rank Fusion.
2. **rerank_candidates** — Cross-encoder reranking, applied selectively
   based on classified intent (fact/pattern/reflection benefit; temporal
   state queries would be hurt).
3. **expand_candidates** — Entity resolution → PMI query expansion →
   graph expansion → intent-specific supplementary candidates.

The scoring pass (in ``application.scoring``) operates on the output of
this pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ncms.application.entity_extraction_mode import use_gliner_entities
from ncms.application.label_cache import load_cached_labels
from ncms.domain.entity_extraction import (
    TEMPORAL_LABELS,
    add_temporal_labels,
    resolve_labels,
)
from ncms.domain.intent import IntentResult, QueryIntent
from ncms.domain.temporal.normalizer import (
    NormalizedInterval,
    RawSpan,
    merge_intervals,
    normalize_spans,
)

if TYPE_CHECKING:
    from ncms.config import NCMSConfig
    from ncms.domain.models import ScoredMemory
    from ncms.domain.protocols import GraphEngine, IndexEngine, MemoryStore
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.reranking.cross_encoder_reranker import (
        CrossEncoderReranker,
    )

logger = logging.getLogger(__name__)


# ── Pure helpers for apply_ordinal_ordering (kept at module scope
# so RetrievalPipeline.apply_ordinal_ordering stays under the D-
# complexity gate).  See that method's docstring for semantics.


def _event_time(sm: ScoredMemory) -> datetime:
    """Extract the sort-key timestamp for a scored memory."""
    mem = sm.memory
    return getattr(mem, "observed_at", None) or getattr(mem, "created_at", None) or datetime.min


def _partition_subjects(
    head: list[ScoredMemory],
    subject_memory_ids: dict[str, str],
    needles: list[str],
) -> tuple[list[ScoredMemory], list[ScoredMemory]]:
    """Split head into (subject_linked, other) using graph + text fallback."""

    def _touches(sm: ScoredMemory) -> bool:
        if sm.memory.id in subject_memory_ids:
            return True
        if not needles:
            return False
        content_lc = (sm.memory.content or "").lower()
        return any(n in content_lc for n in needles)

    subject_linked = [sm for sm in head if _touches(sm)]
    other = [sm for sm in head if not _touches(sm)]
    return subject_linked, other


def _order_single_subject(
    subject_linked: list[ScoredMemory],
    other: list[ScoredMemory],
    tail: list[ScoredMemory],
    ordinal: str,
) -> list[ScoredMemory]:
    """Sort subject-linked slice by date, place at front of head."""
    sorted_linked = sorted(
        subject_linked,
        key=_event_time,
        reverse=(ordinal == "last"),
    )
    return sorted_linked + other + tail


def _order_multi_subject(
    subject_linked: list[ScoredMemory],
    other: list[ScoredMemory],
    tail: list[ScoredMemory],
    subject_entity_ids: list[str],
    subject_memory_ids: dict[str, str],
    ordinal: str,
) -> list[ScoredMemory]:
    """Pick one representative per subject, chronological across reps."""
    reverse = ordinal == "last"
    by_subject: dict[str, list[ScoredMemory]] = {}
    for sm in subject_linked:
        eid = subject_memory_ids.get(sm.memory.id)
        if eid is None:
            continue  # text-fallback-matched — not used for multi-subject
        by_subject.setdefault(eid, []).append(sm)
    reps: list[ScoredMemory] = []
    for eid in subject_entity_ids:
        bucket = by_subject.get(eid)
        if not bucket:
            continue
        reps.append(min(bucket, key=_event_time) if not reverse else max(bucket, key=_event_time))
    reps.sort(key=_event_time)
    rep_ids = {sm.memory.id for sm in reps}
    remaining = sorted(
        (sm for sm in subject_linked if sm.memory.id not in rep_ids),
        key=_event_time,
        reverse=reverse,
    )
    return reps + list(remaining) + other + tail


class RetrievalPipeline:
    """Candidate discovery, reranking, and expansion.

    Dependencies are injected via constructor.  The pipeline is
    stateful only in one narrow sense: it caches the PMI query
    expansion dict between searches; call
    :meth:`invalidate_query_expansion_cache` after a dream cycle
    writes a new dict.
    """

    def __init__(
        self,
        store: MemoryStore,
        index: IndexEngine,
        graph: GraphEngine,
        config: NCMSConfig,
        splade: SpladeEngine | None = None,
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config
        self._splade = splade
        self._reranker = reranker
        # Lazy-loaded PMI expansion dict (invalidated after dream cycles)
        self._query_expansion_dict: dict[str, list[str]] | None = None

    # ── Stage 1: Parallel Retrieval + RRF Fusion ─────────────────────────

    async def retrieve_candidates(
        self,
        query: str,
        domain: str | None,
        emit_stage: Callable,
    ) -> tuple | None:
        """Run BM25 + SPLADE + configured query entities in parallel, fuse via RRF.

        Returns ``None`` if no candidates found, or a tuple of
        ``(fused_candidates, bm25_results, splade_results,
        bm25_scores, splade_scores, query_entity_names,
        parallel_ms)``.
        """
        search_domains = [domain] if domain else []
        cached = await load_cached_labels(self._store, search_domains)
        labels = resolve_labels(search_domains, cached_labels=cached)
        # P1-temporal-experiment: add temporal labels when the range-
        # filter feature is enabled.  Additive — never replaces the
        # entity labels; downstream splits by label type.
        if self._config.temporal_range_filter_enabled:
            labels = add_temporal_labels(labels)

        t0 = time.perf_counter()

        async def _bm25_task() -> list[tuple[str, float]]:
            t = time.perf_counter()
            result = await asyncio.to_thread(
                self._index.search,
                query,
                self._config.tier1_candidates,
            )
            logger.info(
                "[search] BM25 done: %d results (%.0fms)",
                len(result),
                (time.perf_counter() - t) * 1000,
            )
            return result

        async def _splade_task() -> list[tuple[str, float]]:
            if self._splade is None:
                return []
            try:
                t = time.perf_counter()
                result = await asyncio.to_thread(
                    self._splade.search,
                    query,
                    self._config.splade_top_k,
                )
                logger.info(
                    "[search] SPLADE done: %d results (%.0fms)",
                    len(result),
                    (time.perf_counter() - t) * 1000,
                )
                return result
            except Exception:
                logger.warning(
                    "SPLADE search failed, using BM25 only",
                    exc_info=True,
                )
                return []

        async def _entity_task() -> list[dict]:
            if not use_gliner_entities(self._config):
                logger.info(
                    "[search] Query entity extraction skipped: mode=%s",
                    self._config.entity_extraction_mode,
                )
                return []
            from ncms.infrastructure.extraction.gliner_extractor import (
                extract_with_label_budget,
            )

            t = time.perf_counter()
            result = await asyncio.to_thread(
                extract_with_label_budget,
                query,
                labels,
                model_name=self._config.gliner_model,
                threshold=self._config.gliner_threshold,
                cache_dir=self._config.model_cache_dir,
            )
            logger.info(
                "[search] GLiNER done: %d entities (%.0fms)",
                len(result),
                (time.perf_counter() - t) * 1000,
            )
            return result

        logger.info(
            "[search] Starting parallel retrieval: BM25 + SPLADE + entity_extraction(%s)",
            self._config.entity_extraction_mode,
        )
        bm25_results, splade_results, query_entity_names = await asyncio.gather(
            _bm25_task(),
            _splade_task(),
            _entity_task(),
        )
        parallel_ms = (time.perf_counter() - t0) * 1000

        emit_stage(
            "bm25",
            parallel_ms,
            {
                "candidate_count": len(bm25_results),
                "top_score": (round(bm25_results[0][1], 3) if bm25_results else None),
            },
        )
        if splade_results:
            emit_stage(
                "splade",
                parallel_ms,
                {"candidate_count": len(splade_results)},
            )

        # Fuse via Reciprocal Rank Fusion
        if splade_results:
            t0 = time.perf_counter()
            fused_candidates = self.rrf_fuse(
                bm25_results,
                splade_results,
            )
            emit_stage(
                "rrf_fusion",
                (time.perf_counter() - t0) * 1000,
                {"fused_count": len(fused_candidates)},
            )
        else:
            fused_candidates = bm25_results

        if not fused_candidates:
            return None

        bm25_scores = {mid: score for mid, score in bm25_results}
        splade_scores = {mid: score for mid, score in splade_results}

        return (
            fused_candidates,
            bm25_results,
            splade_results,
            bm25_scores,
            splade_scores,
            query_entity_names,
            parallel_ms,
        )

    # ── Temporal helpers (P1-temporal-experiment) ────────────────────────

    @staticmethod
    def split_entity_and_temporal_spans(
        mixed: list[dict],
    ) -> tuple[list[dict], list[RawSpan]]:
        """Partition a mixed GLiNER output into entities and temporal spans.

        ``mixed`` is the list returned by ``extract_entities_gliner``
        (with ``name``, ``type``, ``char_start``, ``char_end`` keys).
        Entities go to the existing entity-linking path unchanged;
        temporal spans are converted to ``RawSpan`` for the normalizer.
        """
        temporal_label_set = {t.lower() for t in TEMPORAL_LABELS}
        entities: list[dict] = []
        spans: list[RawSpan] = []
        for item in mixed:
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
                entities.append(item)
        return entities, spans

    def apply_ordinal_ordering(
        self,
        scored: list[ScoredMemory],
        subject_entity_ids: list[str],
        ordinal: str,
        multi_subject: bool,
        subject_names: list[str] | None = None,
        rerank_k: int = 20,
    ) -> list[ScoredMemory]:
        """Reorder top-K candidates by ``observed_at`` for ordinal queries.

        .. deprecated:: 2026-04
           Superseded by the TLG dispatch path
           (``sequence`` / ``predecessor`` / ``origin`` intents in
           :mod:`ncms.application.tlg.dispatch`).  When
           ``NCMS_TEMPORAL_ENABLED=true`` the grammar layer resolves
           ordinal semantics over the zone graph, which is more
           precise than scalar re-ranking.  Kept for the
           ``temporal_range_filter_enabled=true, temporal_enabled=false``
           deployment path; slated for removal once benchmark parity
           is demonstrated.

        Phase B.2 primitive (see ``docs/retired/p1-temporal-experiment.md``
        §17.2 and §14.3).  Invoked by the search pipeline ONLY when
        the temporal-intent classifier has emitted one of
        ``ORDINAL_SINGLE`` / ``ORDINAL_COMPARE`` / ``ORDINAL_ORDER``
        and GLiNER extracted at least one subject entity.

        Semantics:

        * ``multi_subject=False`` (single-subject "first/last X"):
          partition top-K into subject-linked and other.  Sort the
          subject-linked slice by ``observed_at`` ascending (``first``)
          or descending (``last``).  Place subject-linked slice at
          the front; other candidates stay in relevance order behind.
        * ``multi_subject=True`` (multi-subject compare/order):
          partition by subject.  For each subject, keep its
          earliest/latest memory as a *representative*.  Place
          representatives at the front, ordered chronologically
          (ascending for both ``first`` and ``last`` — we want the
          timeline order, and the ``ordinal`` word tells us which end
          per subject to pick).  Remaining subject-linked candidates
          follow.  Non-subject candidates at the tail.

        Subject matching:

        * Graph linkage — memory is an edge target of one of
          ``subject_entity_ids`` (primary path).
        * Text fallback — memory content contains one of
          ``subject_names`` (case-insensitive, ≥3 chars).  Only used
          for ``multi_subject=False`` because multi-subject text
          matching creates cross-subject bleed (subject A's earliest
          text-match beats subject B's true chronological first).
          Needed because GLiNER is non-deterministic across
          semantically-similar documents.

        Degrade rules (all no-ops, not failures):

        * ``scored`` empty
        * ``subject_entity_ids`` and ``subject_names`` both empty
        * No candidate in top-K touches any subject
        * ``ordinal`` is not ``"first"`` or ``"last"``

        Relies on ``memory.observed_at`` (or ``created_at`` as fallback)
        for the sort key.  Both are populated on every memory by the
        ingestion path.
        """
        if not scored:
            return scored
        if not subject_entity_ids and not subject_names:
            return scored
        if ordinal not in ("first", "last"):
            return scored

        subject_memory_ids = self._build_subject_membership(
            subject_entity_ids,
        )
        needles = self._subject_needles(subject_names, multi_subject)

        k = min(len(scored), rerank_k)
        head = list(scored[:k])
        tail = list(scored[k:])
        subject_linked, other = _partition_subjects(
            head,
            subject_memory_ids,
            needles,
        )
        if not subject_linked:
            return scored

        if multi_subject:
            return (
                _order_multi_subject(
                    subject_linked,
                    other,
                    tail,
                    subject_entity_ids,
                    subject_memory_ids,
                    ordinal,
                )
                or scored
            )
        return _order_single_subject(
            subject_linked,
            other,
            tail,
            ordinal,
        )

    def _build_subject_membership(
        self,
        subject_entity_ids: list[str],
    ) -> dict[str, str]:
        """Map memory_id → subject_entity_id for graph-linked memories."""
        membership: dict[str, str] = {}
        for eid in subject_entity_ids:
            try:
                linked = self._graph.get_memory_ids_for_entity(eid)
            except Exception:
                continue
            for mid in linked:
                # First-subject-wins when a memory links to multiple.
                membership.setdefault(mid, eid)
        return membership

    @staticmethod
    def _subject_needles(
        subject_names: list[str] | None,
        multi_subject: bool,
    ) -> list[str]:
        """Lowercased, min-length-filtered name list for text-fallback.

        Only populated for single-subject mode; multi-subject text
        matching creates cross-subject bleed and is intentionally
        disabled.
        """
        if multi_subject or not subject_names:
            return []
        return [n.strip().lower() for n in subject_names if n and len(n.strip()) >= 3]

    @staticmethod
    def resolve_temporal_range(
        spans: list[RawSpan],
        reference_time: datetime,
    ) -> NormalizedInterval | None:
        """Normalize + merge temporal spans into one query-side range.

        Returns ``None`` when no span resolves cleanly — callers should
        treat that as "no filter applies, fall through to baseline
        retrieval".
        """
        intervals = normalize_spans(spans, reference_time)
        return merge_intervals(intervals)

    async def apply_range_filter(
        self,
        candidates: list[tuple[str, float]],
        query_range: NormalizedInterval,
        missing_range_policy: str = "include",
    ) -> list[tuple[str, float]]:
        """Hard-filter candidates whose ``memory_content_ranges`` row
        overlaps the query range.

        .. deprecated:: 2026-04
           Superseded by the TLG ``range`` intent dispatcher in
           :mod:`ncms.application.tlg.dispatch`, which filters
           subject-scoped ENTITY_STATE nodes by ``observed_at``
           without relying on the GLiNER range-extraction pipeline.
           Kept active for the baseline temporal-only deployment;
           slated for removal after TLG benchmark parity.

        Phase B.4 primitive — implements the paper §5.4 "retrieval
        restricted to items within the relevant time range" mechanism
        LLM-free.  Called from ``MemoryService.search`` only when
        the temporal-intent classifier emits ``RANGE`` or
        ``RELATIVE_ANCHOR`` AND a non-None ``query_range`` exists.

        Overlap semantics:

            Memory range ``[A, B)`` matches query range ``[C, D)``
            iff ``A < D AND B > C``.

        ``missing_range_policy``:

        * ``"include"`` (recall-safe default) — memories without a
          persisted content_range pass the filter.  They weren't
          filterable, so we don't drop them; scoring decides.
        * ``"exclude"`` (precision-safe) — memories without a
          persisted content_range are dropped.  Only memories with a
          known range are kept.

        The filter preserves the input order of the surviving
        candidates.  Called BEFORE scoring, so a tight filter reduces
        downstream scoring cost too.

        Never raises.  Store lookup errors degrade to a no-op (return
        input unchanged) with a warning log.
        """
        if not candidates:
            return candidates
        memory_ids = [mid for mid, _ in candidates]
        try:
            ranges = await self._store.get_content_ranges_batch(
                memory_ids,
            )
        except Exception:
            logger.warning(
                "content_ranges lookup failed; skipping range filter",
                exc_info=True,
            )
            return candidates
        q_start_iso = query_range.start.isoformat()
        q_end_iso = query_range.end.isoformat()
        include_missing = missing_range_policy == "include"

        def _survives(mid: str) -> bool:
            row = ranges.get(mid)
            if row is None:
                return include_missing
            m_start, m_end = row
            # Overlap: half-open intervals, compare ISO-8601 strings
            # lexically (safe because tz-normalized UTC).
            return m_start < q_end_iso and m_end > q_start_iso

        return [(mid, score) for mid, score in candidates if _survives(mid)]

    @staticmethod
    def rrf_fuse(
        bm25_results: list[tuple[str, float]],
        splade_results: list[tuple[str, float]],
        k: int = 60,
    ) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion of two result lists.

        ``RRF score = sum(1 / (k + rank_i))`` across all lists where
        the doc appears.  ``k=60`` is the standard constant from
        Cormack et al. 2009.
        """
        rrf_scores: dict[str, float] = {}

        for rank, (mid, _score) in enumerate(bm25_results):
            rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

        for rank, (mid, _score) in enumerate(splade_results):
            rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

        return sorted(
            rrf_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )

    # ── Stage 2: Cross-Encoder Reranking ─────────────────────────────────

    async def rerank_candidates(
        self,
        query: str,
        fused_candidates: list[tuple[str, float]],
        intent_result: IntentResult | None,
        emit_stage: Callable,
    ) -> tuple[list[tuple[str, float]], dict[str, float]]:
        """Apply cross-encoder reranking when appropriate.

        Only applies for fact-finding, pattern, and reflection intents
        where textual relevance helps.  Skipped for temporal/state
        queries where CE destroys temporal ordering.

        Returns ``(possibly reranked candidates, ce_scores dict)``.
        """
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
        if not _use_ce:
            return fused_candidates, ce_scores

        logger.info(
            "[search] Starting cross-encoder reranking (%d candidates)",
            len(fused_candidates),
        )
        t0 = time.perf_counter()
        rerank_ids = [mid for mid, _ in fused_candidates[: self._config.reranker_top_k]]
        rerank_memories = await self._store.get_memories_batch(
            rerank_ids,
        )
        rerank_pairs = [
            (mid, rerank_memories[mid].content) for mid in rerank_ids if mid in rerank_memories
        ]
        assert self._reranker is not None  # guarded by _use_ce
        reranked = await asyncio.to_thread(
            self._reranker.rerank,
            query,
            rerank_pairs,
            self._config.reranker_output_k,
        )
        ce_scores = {mid: score for mid, score in reranked}
        ce_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "[search] Cross-encoder done: %d\u2192%d results (%.0fms)",
            len(rerank_pairs),
            len(reranked),
            ce_ms,
        )
        emit_stage(
            "cross_encoder_rerank",
            ce_ms,
            {
                "input_count": len(rerank_pairs),
                "output_count": len(reranked),
                "top_score": (round(reranked[0][1], 4) if reranked else None),
            },
        )
        return reranked, ce_scores

    # ── Stage 3: Candidate Expansion ─────────────────────────────────────

    async def expand_candidates(
        self,
        query: str,
        fused_candidates: list[tuple[str, float]],
        query_entity_names: list[dict],
        intent_result: IntentResult | None,
        bm25_scores: dict[str, float],
        parallel_ms: float,
        emit_stage: Callable,
    ) -> tuple[
        list[tuple[str, float]],
        list[str],
        dict[str, list],
    ]:
        """Expand candidates via entities, query terms, and graph.

        Pipeline:

        1. Resolve query entity names to IDs
        2. PMI query expansion (if enabled)
        3. Graph expansion (always on)
        4. Batch node preload
        5. Intent-specific supplementary candidates

        Returns ``(all_candidates, context_entity_ids,
        nodes_by_memory)``.  Side effect: mutates ``bm25_scores`` with
        expansion scores.
        """
        # 1. Entity name resolution
        context_entity_ids = await self._resolve_query_entities(
            query_entity_names,
            parallel_ms,
            emit_stage,
        )

        # 2. PMI query expansion
        await self._apply_query_expansion(
            query,
            context_entity_ids,
            fused_candidates,
            bm25_scores,
            emit_stage,
        )

        # 3. Graph expansion
        fused_ids = {mid for mid, _ in fused_candidates}
        all_candidates = await self._apply_graph_expansion(
            fused_candidates,
            fused_ids,
            emit_stage,
        )

        # 4. Batch node preload
        nodes_by_memory = await self._preload_nodes(
            all_candidates,
            emit_stage,
        )

        # 5. Intent supplementary candidates
        if intent_result and intent_result.intent != QueryIntent.FACT_LOOKUP:
            await self._apply_intent_supplement(
                intent_result,
                context_entity_ids,
                fused_ids,
                all_candidates,
                nodes_by_memory,
                emit_stage,
            )

        return all_candidates, context_entity_ids, nodes_by_memory

    async def _resolve_query_entities(
        self,
        query_entity_names: list[dict],
        parallel_ms: float,
        emit_stage: Callable,
    ) -> list[str]:
        context_entity_ids: list[str] = []
        for qe in query_entity_names:
            eid = self._graph.find_entity_by_name(qe["name"])
            if eid:
                context_entity_ids.append(eid)
                continue
            existing = await self._store.find_entity_by_name(qe["name"])
            if existing:
                context_entity_ids.append(existing.id)
        emit_stage(
            "entity_extraction",
            parallel_ms,
            {
                "query_entities": [e["name"] for e in query_entity_names[:10]],
                "context_entity_count": len(context_entity_ids),
            },
        )
        return context_entity_ids

    async def _apply_query_expansion(
        self,
        query: str,
        context_entity_ids: list[str],
        fused_candidates: list[tuple[str, float]],
        bm25_scores: dict[str, float],
        emit_stage: Callable,
    ) -> None:
        if not (self._config.dream_query_expansion_enabled and context_entity_ids):
            return
        try:
            expansion_terms = await self.get_query_expansion_terms(
                context_entity_ids,
            )
            if not expansion_terms:
                return
            expanded_query = query + " " + " ".join(expansion_terms)
            expanded_bm25 = self._index.search(
                expanded_query,
                limit=self._config.tier1_candidates,
            )
            existing_fused = {mid for mid, _ in fused_candidates}
            novel_from_expansion = 0
            for mid, score in expanded_bm25:
                if mid not in bm25_scores or score > bm25_scores[mid]:
                    bm25_scores[mid] = score
                if mid not in existing_fused:
                    fused_candidates.append((mid, score))
                    existing_fused.add(mid)
                    novel_from_expansion += 1
            emit_stage(
                "query_expansion",
                0,
                {
                    "terms": expansion_terms,
                    "expanded_candidates": len(expanded_bm25),
                    "novel_candidates": novel_from_expansion,
                },
            )
        except Exception:
            logger.debug("Query expansion failed", exc_info=True)

    async def _apply_graph_expansion(
        self,
        fused_candidates: list[tuple[str, float]],
        fused_ids: set[str],
        emit_stage: Callable,
    ) -> list[tuple[str, float]]:
        all_candidates: list[tuple[str, float]] = list(fused_candidates)
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
            if len(novel_ids) > self._config.graph_expansion_max:
                novel_ids = set(
                    list(novel_ids)[: self._config.graph_expansion_max],
                )
            novel_count = len(novel_ids)
            for gid in novel_ids:
                all_candidates.append((gid, 0.0))

        graph_exp_data: dict[str, object] = {
            "entity_pool_size": len(candidate_entity_pool),
            "novel_candidates": novel_count,
            "total_candidates": len(all_candidates),
        }
        if self._config.pipeline_debug and novel_count > 0:
            novel_tuples = all_candidates[-novel_count:]
            graph_exp_data["candidates"] = await self.load_candidate_previews(novel_tuples[:20])
        emit_stage(
            "graph_expansion",
            (time.perf_counter() - t0) * 1000,
            graph_exp_data,
        )
        return all_candidates

    async def _preload_nodes(
        self,
        all_candidates: list[tuple[str, float]],
        emit_stage: Callable,
    ) -> dict[str, list]:
        nodes_by_memory: dict[str, list] = {}
        if not self._config.temporal_enabled:
            return nodes_by_memory
        t0_nodes = time.perf_counter()
        candidate_memory_ids = [mid for mid, _ in all_candidates]
        nodes_by_memory = await self._store.get_memory_nodes_for_memories(
            candidate_memory_ids,
        )
        emit_stage(
            "node_preload",
            (time.perf_counter() - t0_nodes) * 1000,
            {
                "candidate_count": len(candidate_memory_ids),
                "nodes_loaded": sum(len(v) for v in nodes_by_memory.values()),
            },
        )
        return nodes_by_memory

    async def _apply_intent_supplement(
        self,
        intent_result: IntentResult,
        context_entity_ids: list[str],
        fused_ids: set[str],
        all_candidates: list[tuple[str, float]],
        nodes_by_memory: dict[str, list],
        emit_stage: Callable,
    ) -> None:
        t0_supp = time.perf_counter()
        supplement_ids = await self.intent_supplement(
            intent_result,
            context_entity_ids,
            fused_ids,
        )
        for sid in supplement_ids:
            if sid not in fused_ids:
                all_candidates.append((sid, 0.0))
                fused_ids.add(sid)
        if supplement_ids:
            supp_nodes = await self._store.get_memory_nodes_for_memories(
                list(supplement_ids),
            )
            nodes_by_memory.update(supp_nodes)
        emit_stage(
            "intent_supplement",
            (time.perf_counter() - t0_supp) * 1000,
            {
                "intent": intent_result.intent.value,
                "supplement_count": len(supplement_ids),
                "total_candidates": len(all_candidates),
            },
        )

    # ── Intent Supplementary Candidates ──────────────────────────────────

    async def intent_supplement(
        self,
        intent: IntentResult,
        context_entity_ids: list[str],
        already_seen: set[str],
    ) -> set[str]:
        """Generate supplementary candidate memory IDs for specialised intents.

        Returns memory_ids not already in the candidate set.  Dispatches
        to a per-intent fetcher; ``pattern_lookup`` and
        ``strategic_reflection`` have no supplement yet.
        """
        max_supp = self._config.intent_supplement_max
        fetchers = {
            QueryIntent.CURRENT_STATE_LOOKUP: self._supp_current_state,
            QueryIntent.CHANGE_DETECTION: self._supp_change_detection,
            QueryIntent.EVENT_RECONSTRUCTION: self._supp_events,
            QueryIntent.HISTORICAL_LOOKUP: self._supp_historical,
        }
        fetcher = fetchers.get(intent.intent)
        if fetcher is None:
            return set()
        return await fetcher(context_entity_ids, already_seen, max_supp)

    async def _supp_current_state(
        self,
        context_entity_ids: list[str],
        already_seen: set[str],
        max_supp: int,
    ) -> set[str]:
        """Current entity-state memories for the query's entities."""
        supplement: set[str] = set()
        for eid in context_entity_ids:
            states = await self._store.get_entity_states_by_entity(eid)
            for s in states:
                if s.is_current and s.memory_id not in already_seen:
                    supplement.add(s.memory_id)
                    if len(supplement) >= max_supp:
                        return supplement
        return supplement

    async def _supp_change_detection(
        self,
        context_entity_ids: list[str],
        already_seen: set[str],
        max_supp: int,
    ) -> set[str]:
        """All entity-state memories (current + historical) for change queries."""
        supplement: set[str] = set()
        for eid in context_entity_ids:
            states = await self._store.get_entity_states_by_entity(eid)
            for s in states:
                if s.memory_id not in already_seen:
                    supplement.add(s.memory_id)
                    if len(supplement) >= max_supp:
                        return supplement
        return supplement

    async def _supp_events(
        self,
        context_entity_ids: list[str],
        already_seen: set[str],
        max_supp: int,
    ) -> set[str]:
        """Member memories of open episodes for event reconstruction."""
        supplement: set[str] = set()
        episodes = await self._store.get_open_episodes()
        for ep in episodes[:5]:  # Cap episode lookups
            members = await self._store.get_episode_members(ep.id)
            for m in members:
                if m.memory_id not in already_seen:
                    supplement.add(m.memory_id)
                    if len(supplement) >= max_supp:
                        return supplement
        return supplement

    async def _supp_historical(
        self,
        context_entity_ids: list[str],
        already_seen: set[str],
        max_supp: int,
    ) -> set[str]:
        """Recent state changes (last 90 days) for historical queries."""
        from datetime import UTC, datetime, timedelta

        supplement: set[str] = set()
        cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        changes = await self._store.get_state_changes_since(cutoff)
        for c in changes:
            if c.memory_id not in already_seen:
                supplement.add(c.memory_id)
                if len(supplement) >= max_supp:
                    return supplement
        return supplement

    # ── PMI Query Expansion ──────────────────────────────────────────────

    def invalidate_query_expansion_cache(self) -> None:
        """Clear cached expansion dict so next search reloads from DB.

        Call after a dream cycle writes a new expansion dict.
        """
        self._query_expansion_dict = None

    async def get_query_expansion_terms(
        self,
        context_entity_ids: list[str],
    ) -> list[str]:
        """Look up PMI-learned expansion terms for the query's entities.

        Loads the expansion dict from consolidation_state on first call
        (cached until :meth:`invalidate_query_expansion_cache` is
        called).  Returns a flat list of expansion term strings.
        """
        import json as _json

        # Lazy-load expansion dict
        if self._query_expansion_dict is None:
            raw = await self._store.get_consolidation_value(
                "query_expansion_dict",
            )
            if raw:
                try:
                    self._query_expansion_dict = _json.loads(raw)
                except Exception:
                    self._query_expansion_dict = {}
            else:
                self._query_expansion_dict = {}

        if not self._query_expansion_dict:
            return []

        # Round-robin allocation: each entity gets a fair share of
        # expansion slots (prevents first entity from hogging them).
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

    # ── Debug Previews ───────────────────────────────────────────────────

    async def load_candidate_previews(
        self,
        candidates: list[tuple[str, float]],
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Load content previews for candidate IDs (debug mode only)."""
        result: list[dict[str, object]] = []
        for mid, score in candidates[:limit]:
            memory = await self._store.get_memory(mid)
            result.append(
                {
                    "id": mid,
                    "score": round(score, 3),
                    "content": (memory.content[:120] if memory else "(not found)"),
                }
            )
        return result
