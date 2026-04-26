"""Search-pipeline diagnostics + finalisation helpers.

Extracted from :class:`MemoryService` in the Phase D MI cleanup so
the orchestrator stays under the B+ maintainability bar.  These
free functions used to be private methods (``_diag_*``,
``_search_post_score_finalize``, ``_log_search_for_dream_cycle``,
``_emit_query_diagnostic``).

Public entry points:

* :func:`search_post_score_finalize` — tail of ``search`` after
  scoring.  Handles ordinal reordering, slicing, access logging,
  pipeline event emission, dream-cycle logging, and TLG composition.
* :func:`emit_query_diagnostic` — comprehensive per-query diagnostic
  event with signal coverage + HTMG stats + top result breakdown.

The smaller helpers (``signal_coverage``, ``htmg_subject_stats``,
``top_breakdown``) are exported for unit-test use.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ncms.domain.models import AccessRecord, ScoredMemory, SearchLogEntry

logger = logging.getLogger(__name__)


def signal_coverage(scored: list[ScoredMemory]) -> dict[str, int]:
    """Count candidates with non-zero contribution per signal."""
    return {
        "intent_alignment": sum(1 for s in scored if s.intent_alignment_contrib != 0.0),
        "state_change_alignment": sum(1 for s in scored if s.state_change_alignment_contrib != 0.0),
        "role_grounding": sum(1 for s in scored if s.role_grounding_contrib != 0.0),
        "hierarchy_bonus": sum(1 for s in scored if s.hierarchy_bonus != 0.0),
        "temporal": sum(1 for s in scored if s.temporal_score != 0.0),
        "graph": sum(1 for s in scored if s.spreading != 0.0),
        "reconciliation_penalty": sum(1 for s in scored if s.reconciliation_penalty != 0.0),
    }


async def htmg_subject_stats(
    *,
    store,
    config,
    context_entity_ids: list[str],
) -> dict[str, int]:
    """L2 / supersession / causal edge counts for query subjects."""
    if not (config.temporal_enabled and context_entity_ids):
        return {}
    try:
        l2_count = 0
        sup_count = 0
        for eid in context_entity_ids[:10]:
            states = await store.get_entity_states_by_entity(eid)
            l2_count += len(states)
            sup_count += sum(1 for s in states if not s.is_current)
        return {
            "l2_entity_states": l2_count,
            "supersession_chain_size": sup_count,
            "causal_edges": 0,  # CTLG fills this
        }
    except Exception:
        logger.debug("htmg_subject_stats lookup failed", exc_info=True)
        return {}


def top_breakdown(results: list[ScoredMemory]) -> dict[str, object] | None:
    """Full signal vector for the rank-1 result, or None if empty."""
    if not results:
        return None
    top = results[0]
    return {
        "memory_id": top.memory.id,
        "content_preview": top.memory.content[:120],
        "node_types": top.node_types,
        "bm25_raw": round(top.bm25_score, 3),
        "splade_raw": round(top.splade_score, 3),
        "graph_raw": round(top.spreading, 3),
        "h_bonus": round(top.hierarchy_bonus, 3),
        "ia_contrib": round(top.intent_alignment_contrib, 3),
        "sc_contrib": round(top.state_change_alignment_contrib, 3),
        "rg_contrib": round(top.role_grounding_contrib, 3),
        "temporal": round(top.temporal_score, 3),
        "penalty": round(top.reconciliation_penalty, 3),
        "total": round(top.total_activation, 3),
        "is_superseded": top.is_superseded,
        "has_conflicts": top.has_conflicts,
    }


async def log_search_for_dream_cycle(
    *,
    store,
    query: str,
    query_entity_names: list[dict],
    results: list[ScoredMemory],
    agent_id: str | None,
) -> None:
    """Phase 8 — record query→result associations for PMI computation."""
    try:
        entity_names_for_log = [
            e["name"] for e in (query_entity_names or []) if isinstance(e, dict) and e.get("name")
        ]
        await store.log_search(
            SearchLogEntry(
                query=query,
                query_entities=entity_names_for_log,
                returned_ids=[r.memory.id for r in results],
                agent_id=agent_id,
            )
        )
    except Exception:
        logger.debug("Failed to log search for dream cycle", exc_info=True)


async def search_post_score_finalize(
    *,
    store,
    event_log,
    config,
    apply_ordinal_fn: Callable,
    retrieve_lg_fn: Callable,
    query: str,
    limit: int,
    scored: list[ScoredMemory],
    query_entity_names: list[dict],
    context_entity_ids: list[str],
    temporal_ref: object | None,
    agent_id: str | None,
    pipeline_start: float,
    emit_stage: Callable,
    stage_candidates_out: dict[str, list[str]] | None,
) -> tuple[list[ScoredMemory], bool, float | None]:
    """Tail of ``search`` after scoring.

    Returns ``(results, grammar_composed, grammar_confidence)``.
    """
    # 1. Ordinal reordering — pure, fast, no-op outside ordinal intents.
    subject_names = [
        qe.get("name", "") for qe in query_entity_names if isinstance(qe, dict) and qe.get("name")
    ]
    scored = apply_ordinal_fn(
        query,
        scored,
        temporal_ref,
        context_entity_ids,
        subject_names,
        emit_stage,
    )

    # 2 + 3. Slice + access log.
    results = scored[:limit]
    for sm in results:
        await store.log_access(
            AccessRecord(
                memory_id=sm.memory.id,
                accessing_agent=agent_id,
                query_context=query,
            ),
        )

    # 4 + 5. Pipeline complete + memory.searched events.
    total_ms = (time.perf_counter() - pipeline_start) * 1000
    emit_stage(
        "complete",
        total_ms,
        {
            "result_count": len(results),
            "total_candidates_evaluated": len(scored),
            "top_score": (round(results[0].total_activation, 3) if results else None),
            "total_duration_ms": round(total_ms, 2),
        },
    )
    event_log.memory_searched(
        query=query,
        result_count=len(results),
        top_score=results[0].total_activation if results else None,
        agent_id=agent_id,
    )

    # 6. Dream-cycle search log.
    if config.dream_cycle_enabled and results:
        await log_search_for_dream_cycle(
            store=store,
            query=query,
            query_entity_names=query_entity_names,
            results=results,
            agent_id=agent_id,
        )

    # 7. TLG composition.
    grammar_composed = False
    grammar_confidence: float | None = None
    if config.temporal_enabled:
        from ncms.application.tlg.composition import compose_grammar_with_results

        (
            results,
            grammar_composed,
            grammar_confidence,
        ) = await compose_grammar_with_results(
            store=store,
            event_log=event_log,
            retrieve_lg_fn=retrieve_lg_fn,
            query=query,
            results=results,
            limit=limit,
        )

    # 8. Capture final-stage candidates.
    if stage_candidates_out is not None:
        stage_candidates_out["returned"] = [r.memory.id for r in results]
    return results, grammar_composed, grammar_confidence


async def emit_query_diagnostic(
    *,
    store,
    event_log,
    config,
    query: str,
    intent_result: object | None,
    query_entity_names: list[dict],
    context_entity_ids: list[str],
    temporal_ref: object | None,
    grammar_composed: bool,
    grammar_confidence: float | None,
    bm25_count: int,
    splade_count: int,
    fused_count: int,
    expanded_count: int,
    scored: list[ScoredMemory],
    results: list[ScoredMemory],
    total_ms: float,
    agent_id: str | None,
) -> None:
    """Build and emit the comprehensive per-query diagnostic.

    See :meth:`EventLog.query_diagnostic` for the payload spec.
    Always-on (not gated by ``pipeline_debug``).
    """
    coverage = signal_coverage(scored)
    htmg_stats = await htmg_subject_stats(
        store=store, config=config, context_entity_ids=context_entity_ids
    )
    top = top_breakdown(results)

    intent_str = (
        getattr(getattr(intent_result, "intent", None), "value", None)
        if intent_result is not None
        else None
    )
    intent_conf = getattr(intent_result, "confidence", None) if intent_result is not None else None
    temporal_ref_str = repr(temporal_ref)[:200] if temporal_ref is not None else None
    query_names = [
        qe["name"] for qe in query_entity_names if isinstance(qe, dict) and qe.get("name")
    ]

    event_log.query_diagnostic(
        query=query,
        intent=intent_str,
        intent_confidence=intent_conf,
        query_entities=query_names,
        resolved_entity_ids=list(context_entity_ids),
        temporal_ref=temporal_ref_str,
        grammar_composed=grammar_composed,
        grammar_confidence=grammar_confidence,
        candidate_counts={
            "bm25": bm25_count,
            "splade": splade_count,
            "rrf_fused": fused_count,
            "expanded": expanded_count,
            "scored": len(scored),
            "returned": len(results),
        },
        signal_coverage=coverage,
        htmg_subject_stats=htmg_stats,
        top_breakdown=top,
        result_count=len(results),
        total_ms=total_ms,
        agent_id=agent_id,
    )

    # One-line INFO log for grep-ability.  Format is stable so
    # downstream tooling (CTLG verification harness, dashboards) can
    # parse it.
    sig_compact = "/".join(
        str(coverage[k])
        for k in (
            "intent_alignment",
            "state_change_alignment",
            "role_grounding",
            "hierarchy_bonus",
            "temporal",
            "graph",
            "reconciliation_penalty",
        )
    )
    top_compact = f"{top['memory_id']}:{top['total']}" if top else "none"
    logger.info(
        "[diag] q=%r intent=%s/%s ents=%d cnt=%d/%d/%d/%d/%d/%d sigcov=%s gram=%s top=%s ms=%.1f",
        query[:80],
        intent_str,
        intent_conf,
        len(query_names),
        bm25_count,
        splade_count,
        fused_count,
        expanded_count,
        len(scored),
        len(results),
        sig_compact,
        f"y@{grammar_confidence:.2f}" if grammar_composed else "n",
        top_compact,
        total_ms,
    )
