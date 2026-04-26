"""Enrichment pipeline: recall bonuses and RecallResult decoration.

Two flavours of work:

1. **Recall bonuses** — for non-fact intents (current-state, historical,
   change-detection, event-reconstruction), fetch structured results
   that BM25 may have missed and prepend them to the base set.
2. **Enrichment** — for every RecallResult, populate the
   ``RecallContext`` with entity state snapshots, episode membership,
   causal chains, and relevant document sections.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ncms.domain.intent import QueryIntent
from ncms.domain.models import (
    DocumentSectionContext,
    EdgeType,
    EntityStateSnapshot,
    EpisodeContext,
    RecallContext,
    RecallResult,
    ScoredMemory,
)

if TYPE_CHECKING:
    from ncms.application.document_service import DocumentService
    from ncms.domain.protocols import GraphEngine, MemoryStore

logger = logging.getLogger(__name__)


class EnrichmentPipeline:
    """Recall bonuses and RecallResult context decoration.

    Dependencies are injected via constructor.  All methods are pure
    wrt their inputs — the pipeline carries no mutable state.
    """

    def __init__(
        self,
        store: MemoryStore,
        graph: GraphEngine,
        document_service: DocumentService | None = None,
    ) -> None:
        self._store = store
        self._graph = graph
        self._document_service = document_service

    # ── Recall bonus helpers (layered on top of BM25 base) ──────────────

    async def recall_structured_state(
        self,
        entity_ids: list[str],
        intent: QueryIntent,
        seen_memory_ids: set[str],
    ) -> list[RecallResult]:
        """Fetch state-graph bonus results for state/historical/change intents.

        Returns memories from the entity state graph that BM25 may have
        missed.  Only includes memories not already in ``seen_memory_ids``.
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
                bonus.append(
                    RecallResult(
                        memory=scored,
                        context=RecallContext(
                            entity_states=[
                                EntityStateSnapshot(
                                    entity_id=eid,
                                    entity_name=(self._graph.get_entity_name(eid) or eid),
                                    state_key=meta.state_key or "",
                                    state_value=meta.state_value or "",
                                    is_current=sn.is_current,
                                    observed_at=sn.observed_at,
                                )
                            ],
                        ),
                        retrieval_path=path,
                    )
                )

        return bonus

    async def recall_episode_bonus(
        self,
        scored: list[ScoredMemory],
        seen_memory_ids: set[str],
    ) -> list[RecallResult]:
        """Expand episode abstracts from search results into member memories.

        For ``EVENT_RECONSTRUCTION``: find episode summaries in the BM25
        results, expand via DERIVED_FROM/SUMMARIZES edges to find member
        memories that BM25 may have missed.
        """
        bonus: list[RecallResult] = []
        abstracts = [s for s in scored if "abstract" in (s.node_types or [])]

        for abstract in abstracts[:5]:
            nodes = await self._store.get_memory_nodes_for_memory(
                abstract.memory.id,
            )
            for node in nodes:
                try:
                    edges = await self._store.get_graph_edges(node.id)
                except Exception:
                    continue
                for edge in edges:
                    if edge.edge_type not in (
                        "derived_from",
                        "summarizes",
                    ):
                        continue
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
                    bonus.append(
                        RecallResult(
                            memory=sm,
                            retrieval_path="episode_expansion_bonus",
                        )
                    )

        return bonus

    # ── Context enrichment (per-result decoration) ──────────────────────

    async def enrich_existing_results(
        self,
        results: list[RecallResult],
    ) -> list[RecallResult]:
        """Enrich each RecallResult with episode + entity state + causal context.

        Operates in batch where possible to minimize DB round-trips.
        """
        if not results:
            return results

        # Batch preload memory nodes for all results
        memory_ids = [r.memory.memory.id for r in results]
        nodes_batch = await self._store.get_memory_nodes_for_memories(
            memory_ids,
        )

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
        """Populate entity state snapshots on a RecallResult.

        Caps at 10 entities per result.
        """
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
                ep_node = await self._store.get_memory_node(
                    node.parent_id,
                )
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
                sibling_ids=[m.memory_id for m in members if m.memory_id != memory_id],
                summary=summary_text,
            )
            break

    async def _enrich_causal_chain(
        self,
        result: RecallResult,
        nodes: list,
    ) -> None:
        """Populate causal-chain edges (supersedes, derived_from, etc.)."""
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

    async def _find_episode_summary(
        self,
        episode_node_id: str,
    ) -> str | None:
        """Find an episode summary abstract that SUMMARIZES this episode."""
        try:
            edges = await self._store.get_graph_edges(episode_node_id)
        except Exception:
            return None
        for edge in edges:
            if edge.edge_type in ("summarizes",):
                # The source of a SUMMARIZES edge is the abstract
                summary_node = await self._store.get_memory_node(
                    edge.source_id,
                )
                if summary_node:
                    memory = await self._store.get_memory(
                        summary_node.memory_id,
                    )
                    if memory:
                        return memory.content[:500]
        return None

    # ── Document profile expansion ──────────────────────────────────────

    async def expand_document_sections(
        self,
        results: list[RecallResult],
        query: str,
        max_sections: int = 3,
    ) -> list[RecallResult]:
        """Expand document profile memories into relevant child sections.

        When a ``RecallResult`` has a memory with ``structured.doc_id``,
        fetches child sections from the document store, scores them
        against the query using keyword overlap, and adds the top N
        as ``DocumentSectionContext`` entries.
        """
        if not self._document_service:
            return results

        query_terms = set(query.lower().split())

        for result in results:
            await self._expand_single_result(
                result,
                query_terms,
                max_sections,
            )

        return results

    async def _expand_single_result(
        self,
        result: RecallResult,
        query_terms: set[str],
        max_sections: int,
    ) -> None:
        memory = result.memory.memory
        structured = memory.structured
        if not structured or "doc_id" not in structured:
            return

        doc_id = structured["doc_id"]
        try:
            assert self._document_service is not None
            parent_doc = await self._document_service.get_document(doc_id)
            if not parent_doc:
                return

            children = await self._document_service.get_children_documents(
                doc_id,
            )
            if not children:
                return

            # Score sections against query using keyword overlap
            scored_sections: list[tuple[float, int, Any]] = []
            for child in children:
                child_terms = set(child.content.lower().split())
                if not child_terms:
                    continue
                overlap = len(query_terms & child_terms)
                score = overlap / max(len(query_terms), 1)
                section_idx = (child.metadata or {}).get(
                    "section_index",
                    0,
                )
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
                doc_id,
                len(children),
                len(top_sections),
            )
        except Exception as exc:
            logger.warning(
                "[recall] Failed to expand document profile %s: %s",
                doc_id,
                exc,
            )
