"""Grammar ∨ BM25 composition — moves a confident TLG trace's answer
to rank 1 ahead of BM25 results, preserving every other score field.

Extracted from :class:`MemoryService` in the Phase D MI cleanup so
the orchestrator stays under the B+ maintainability bar.  These
helpers were previously private methods (``_compose_grammar_with_results``,
``_compose_trace_onto_scored``, ``_resolve_node_to_memory_id``) and
remain pure pass-through glue between the dispatcher and scored
results.

Failure semantics:

* :func:`compose_grammar_with_results` never raises — exceptions
  inside ``retrieve_lg`` or store fetches log + return the input
  list unchanged.  Callers depend on strict graceful degradation:
  TLG can only improve search, never break it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ncms.domain.models import Memory, ScoredMemory

logger = logging.getLogger(__name__)


async def _resolve_node_to_memory_id(
    *,
    store,
    node_id: str | None,
) -> str | None:
    """Map a MemoryNode ID to its backing Memory ID, or ``None``."""
    if node_id is None:
        return None
    try:
        node = await store.get_memory_node(node_id)
    except Exception:
        logger.debug("TLG: failed to fetch memory node %s", node_id, exc_info=True)
        return None
    return node.memory_id if node is not None else None


async def _compose_trace_onto_scored(
    *,
    store,
    grammar_answer: str | None,
    zone_context: list[str],
    results: list[ScoredMemory],
) -> list[ScoredMemory]:
    """Reorder ``results`` so the grammar answer + zone context lead.

    Scores are preserved on items already in the list; new items
    fetched from the store get a sentinel score so callers sorting
    by ``total_activation`` keep them at the top.
    """
    by_memory_id = {sm.memory.id: sm for sm in results}
    grammar_memory_id = await _resolve_node_to_memory_id(store=store, node_id=grammar_answer)
    zone_memory_ids: list[str] = []
    for node_id in zone_context:
        mid = await _resolve_node_to_memory_id(store=store, node_id=node_id)
        if mid is not None:
            zone_memory_ids.append(mid)

    max_activation = max((sm.total_activation for sm in results), default=0.0)
    sentinel = max_activation + 1.0

    composed: list[ScoredMemory] = []
    placed: set[str] = set()

    async def _emit(memory_id: str | None) -> None:
        if memory_id is None or memory_id in placed:
            return
        existing = by_memory_id.get(memory_id)
        if existing is not None:
            composed.append(existing.model_copy(update={"total_activation": sentinel}))
        else:
            mem: Memory | None = await store.get_memory(memory_id)
            if mem is None:
                return
            composed.append(ScoredMemory(memory=mem, total_activation=sentinel))
        placed.add(memory_id)

    await _emit(grammar_memory_id)
    for mid in zone_memory_ids:
        await _emit(mid)

    for sm in results:
        if sm.memory.id not in placed:
            composed.append(sm)
            placed.add(sm.memory.id)
    return composed


async def compose_grammar_with_results(
    *,
    store,
    event_log,
    retrieve_lg_fn: Callable[[str], Awaitable],
    query: str,
    results: list[ScoredMemory],
    limit: int,
) -> tuple[list[ScoredMemory], bool, float | None]:
    """Apply the grammar ∨ BM25 invariant to ``search`` results.

    Returns ``(results, did_compose, confidence)``:
      * ``results``     — possibly reordered list.
      * ``did_compose`` — True iff the trace was confident and
        displaced the BM25 top-1.
      * ``confidence``  — trace confidence when composition fired,
        else ``None``.
    """
    try:
        trace = await retrieve_lg_fn(query)
    except Exception:
        logger.warning("TLG dispatch failed during search", exc_info=True)
        return results, False, None
    if not trace.has_confident_answer():
        return results, False, None

    composed = await _compose_trace_onto_scored(
        store=store,
        grammar_answer=trace.grammar_answer,
        zone_context=trace.zone_context,
        results=results,
    )
    composed = composed[:limit] if limit else composed
    try:
        grammar_memory_id = await _resolve_node_to_memory_id(
            store=store, node_id=trace.grammar_answer
        )
        event_log.grammar_composed(  # type: ignore[attr-defined]
            query=query,
            intent=trace.intent.kind,
            confidence=trace.confidence.value,
            grammar_answer_memory_id=grammar_memory_id,
            zone_context_count=len(trace.zone_context),
            bm25_count_before=len(results),
            composed_count=len(composed),
        )
    except Exception:  # pragma: no cover — defensive guard
        pass
    return composed, True, trace.confidence.value
