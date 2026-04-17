"""Retrieval pipeline: candidate discovery, reranking, and expansion.

Three stages — each a public method on ``RetrievalPipeline``:

1. **retrieve_candidates** — Parallel BM25 + SPLADE + GLiNER extraction,
   fused via Reciprocal Rank Fusion.
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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ncms.domain.entity_extraction import resolve_labels
from ncms.domain.intent import IntentResult, QueryIntent

if TYPE_CHECKING:
    from ncms.config import NCMSConfig
    from ncms.domain.protocols import GraphEngine, IndexEngine, MemoryStore
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.reranking.cross_encoder_reranker import (
        CrossEncoderReranker,
    )

logger = logging.getLogger(__name__)


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
        get_cached_labels: (
            Callable[[list[str]], Awaitable[dict]] | None
        ) = None,
    ) -> None:
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config
        self._splade = splade
        self._reranker = reranker
        self._get_cached_labels = get_cached_labels
        # Lazy-loaded PMI expansion dict (invalidated after dream cycles)
        self._query_expansion_dict: dict[str, list[str]] | None = None

    # ── Stage 1: Parallel Retrieval + RRF Fusion ─────────────────────────

    async def retrieve_candidates(
        self,
        query: str,
        domain: str | None,
        emit_stage: Callable,
    ) -> tuple | None:
        """Run BM25 + SPLADE + GLiNER in parallel, fuse via RRF.

        Returns ``None`` if no candidates found, or a tuple of
        ``(fused_candidates, bm25_results, splade_results,
        bm25_scores, splade_scores, query_entity_names,
        parallel_ms)``.
        """
        from ncms.infrastructure.extraction.gliner_extractor import (
            extract_entities_gliner,
        )

        search_domains = [domain] if domain else []
        cached = (
            await self._get_cached_labels(search_domains)
            if self._get_cached_labels else {}
        )
        labels = resolve_labels(search_domains, cached_labels=cached)

        t0 = time.perf_counter()

        async def _bm25_task() -> list[tuple[str, float]]:
            t = time.perf_counter()
            result = await asyncio.to_thread(
                self._index.search, query,
                self._config.tier1_candidates,
            )
            logger.info(
                "[search] BM25 done: %d results (%.0fms)",
                len(result), (time.perf_counter() - t) * 1000,
            )
            return result

        async def _splade_task() -> list[tuple[str, float]]:
            if self._splade is None:
                return []
            try:
                t = time.perf_counter()
                result = await asyncio.to_thread(
                    self._splade.search, query,
                    self._config.splade_top_k,
                )
                logger.info(
                    "[search] SPLADE done: %d results (%.0fms)",
                    len(result), (time.perf_counter() - t) * 1000,
                )
                return result
            except Exception:
                logger.warning(
                    "SPLADE search failed, using BM25 only",
                    exc_info=True,
                )
                return []

        async def _entity_task() -> list[dict]:
            t = time.perf_counter()
            result = await asyncio.to_thread(
                extract_entities_gliner, query,
                model_name=self._config.gliner_model,
                threshold=self._config.gliner_threshold,
                labels=labels,
                cache_dir=self._config.model_cache_dir,
            )
            logger.info(
                "[search] GLiNER done: %d entities (%.0fms)",
                len(result), (time.perf_counter() - t) * 1000,
            )
            return result

        logger.info(
            "[search] Starting parallel retrieval: "
            "BM25 + SPLADE + GLiNER",
        )
        bm25_results, splade_results, query_entity_names = (
            await asyncio.gather(
                _bm25_task(), _splade_task(), _entity_task(),
            )
        )
        parallel_ms = (time.perf_counter() - t0) * 1000

        emit_stage("bm25", parallel_ms, {
            "candidate_count": len(bm25_results),
            "top_score": (
                round(bm25_results[0][1], 3) if bm25_results else None
            ),
        })
        if splade_results:
            emit_stage(
                "splade", parallel_ms,
                {"candidate_count": len(splade_results)},
            )

        # Fuse via Reciprocal Rank Fusion
        if splade_results:
            t0 = time.perf_counter()
            fused_candidates = self.rrf_fuse(
                bm25_results, splade_results,
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
            fused_candidates, bm25_results, splade_results,
            bm25_scores, splade_scores, query_entity_names,
            parallel_ms,
        )

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
            rrf_scores[mid] = (
                rrf_scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
            )

        for rank, (mid, _score) in enumerate(splade_results):
            rrf_scores[mid] = (
                rrf_scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
            )

        return sorted(
            rrf_scores.items(), key=lambda x: x[1], reverse=True,
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
            and (
                intent_result is None
                or intent_result.intent in ce_intents
            )
        )
        ce_scores: dict[str, float] = {}
        if not _use_ce:
            return fused_candidates, ce_scores

        logger.info(
            "[search] Starting cross-encoder reranking "
            "(%d candidates)",
            len(fused_candidates),
        )
        t0 = time.perf_counter()
        rerank_ids = [
            mid for mid, _ in fused_candidates[
                :self._config.reranker_top_k
            ]
        ]
        rerank_memories = await self._store.get_memories_batch(
            rerank_ids,
        )
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
        ce_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "[search] Cross-encoder done: %d\u2192%d results "
            "(%.0fms)",
            len(rerank_pairs), len(reranked), ce_ms,
        )
        emit_stage("cross_encoder_rerank", ce_ms, {
            "input_count": len(rerank_pairs),
            "output_count": len(reranked),
            "top_score": (
                round(reranked[0][1], 4) if reranked else None
            ),
        })
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
        list[tuple[str, float]], list[str], dict[str, list],
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
            query_entity_names, parallel_ms, emit_stage,
        )

        # 2. PMI query expansion
        await self._apply_query_expansion(
            query, context_entity_ids, fused_candidates,
            bm25_scores, emit_stage,
        )

        # 3. Graph expansion
        fused_ids = {mid for mid, _ in fused_candidates}
        all_candidates = await self._apply_graph_expansion(
            fused_candidates, fused_ids, emit_stage,
        )

        # 4. Batch node preload
        nodes_by_memory = await self._preload_nodes(
            all_candidates, emit_stage,
        )

        # 5. Intent supplementary candidates
        if (
            intent_result
            and intent_result.intent != QueryIntent.FACT_LOOKUP
        ):
            await self._apply_intent_supplement(
                intent_result, context_entity_ids,
                fused_ids, all_candidates, nodes_by_memory,
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
        emit_stage("entity_extraction", parallel_ms, {
            "query_entities": [
                e["name"] for e in query_entity_names[:10]
            ],
            "context_entity_count": len(context_entity_ids),
        })
        return context_entity_ids

    async def _apply_query_expansion(
        self,
        query: str,
        context_entity_ids: list[str],
        fused_candidates: list[tuple[str, float]],
        bm25_scores: dict[str, float],
        emit_stage: Callable,
    ) -> None:
        if not (
            self._config.dream_query_expansion_enabled
            and context_entity_ids
        ):
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
                if (
                    mid not in bm25_scores
                    or score > bm25_scores[mid]
                ):
                    bm25_scores[mid] = score
                if mid not in existing_fused:
                    fused_candidates.append((mid, score))
                    existing_fused.add(mid)
                    novel_from_expansion += 1
            emit_stage("query_expansion", 0, {
                "terms": expansion_terms,
                "expanded_candidates": len(expanded_bm25),
                "novel_candidates": novel_from_expansion,
            })
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
                    list(novel_ids)[
                        :self._config.graph_expansion_max
                    ],
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
            graph_exp_data["candidates"] = (
                await self.load_candidate_previews(novel_tuples[:20])
            )
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
        if not (
            self._config.intent_classification_enabled
            or self._config.reconciliation_enabled
        ):
            return nodes_by_memory
        t0_nodes = time.perf_counter()
        candidate_memory_ids = [mid for mid, _ in all_candidates]
        nodes_by_memory = (
            await self._store.get_memory_nodes_for_memories(
                candidate_memory_ids,
            )
        )
        emit_stage(
            "node_preload",
            (time.perf_counter() - t0_nodes) * 1000,
            {
                "candidate_count": len(candidate_memory_ids),
                "nodes_loaded": sum(
                    len(v) for v in nodes_by_memory.values()
                ),
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
            intent_result, context_entity_ids, fused_ids,
        )
        for sid in supplement_ids:
            if sid not in fused_ids:
                all_candidates.append((sid, 0.0))
                fused_ids.add(sid)
        if supplement_ids:
            supp_nodes = (
                await self._store.get_memory_nodes_for_memories(
                    list(supplement_ids),
                )
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
        self, context_entity_ids: list[str],
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
        n_entities = (
            len(context_entity_ids) if context_entity_ids else 1
        )
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
            result.append({
                "id": mid,
                "score": round(score, 3),
                "content": (
                    memory.content[:120] if memory else "(not found)"
                ),
            })
        return result
