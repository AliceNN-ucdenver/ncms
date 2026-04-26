"""Pre-/post-scoring helpers for :meth:`MemoryService.search`.

Extracted from ``memory_service.py`` in the Phase D MI cleanup so the
orchestrator stays under the B+ maintainability bar.

Public entry points:

* :func:`extract_query_range`         — Phase A temporal split.
* :func:`apply_ordinal_if_eligible`   — Phase B.2 ordinal reorder.
* :func:`apply_range_filter_if_eligible` — Phase B.4 range filter.
* :func:`classify_search_intent`      — exemplar / keyword / LLM
  intent classification with override + low-confidence fallback.

Each helper takes the collaborators it needs as kwargs (``config``,
``retrieval`` pipeline, ``intent_classifier``), keeping every
side-effect explicit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from ncms.domain.intent import IntentResult, QueryIntent, classify_intent


def extract_query_range(
    *,
    config,
    retrieval,
    query_entity_names: list[dict],
    reference_time: datetime,
    emit_stage: Callable,
) -> tuple[list[dict], object | None]:
    """Split mixed GLiNER output into (entity names, query range).

    No-op when ``temporal_range_filter_enabled`` is False.
    """
    if not config.temporal_range_filter_enabled:
        return query_entity_names, None
    entity_names, temporal_spans = retrieval.split_entity_and_temporal_spans(
        query_entity_names,
    )
    if not temporal_spans:
        return entity_names, None
    query_range = retrieval.resolve_temporal_range(temporal_spans, reference_time)
    emit_stage(
        "temporal_range_extracted",
        0.0,
        {
            "span_count": len(temporal_spans),
            "spans": [s.text for s in temporal_spans[:10]],
            "range_start": (query_range.start.isoformat() if query_range else None),
            "range_end": (query_range.end.isoformat() if query_range else None),
            "confidence": (query_range.confidence if query_range else None),
        },
    )
    return entity_names, query_range


def apply_ordinal_if_eligible(
    *,
    config,
    retrieval,
    query: str,
    scored: list,
    temporal_ref: object | None,
    context_entity_ids: list[str],
    subject_names: list[str],
    emit_stage: Callable,
) -> list:
    """Phase B.2 — reorder by observed_at when intent is ordinal."""
    from ncms.domain.temporal.intent import (
        TemporalIntent,
        classify_temporal_intent,
    )

    if not config.temporal_range_filter_enabled:
        return scored
    if not scored:
        return scored
    ordinal = getattr(temporal_ref, "ordinal", None) if temporal_ref else None
    has_range = bool(temporal_ref) and bool(
        getattr(temporal_ref, "range_start", None) or getattr(temporal_ref, "range_end", None),
    )
    has_relative = bool(temporal_ref) and bool(
        getattr(temporal_ref, "recency_bias", False),
    )
    intent = classify_temporal_intent(
        query,
        ordinal=ordinal,
        has_range=has_range,
        has_relative=has_relative,
        subject_count=len(context_entity_ids),
    )
    emit_stage(
        "temporal_intent_classified",
        0.0,
        {
            "intent": intent.value,
            "subject_count": len(context_entity_ids),
            "ordinal": ordinal,
        },
    )
    if intent == TemporalIntent.ORDINAL_SINGLE and ordinal:
        return retrieval.apply_ordinal_ordering(
            scored,
            subject_entity_ids=context_entity_ids,
            subject_names=subject_names,
            ordinal=ordinal,
            multi_subject=False,
        )
    if intent in (TemporalIntent.ORDINAL_COMPARE, TemporalIntent.ORDINAL_ORDER) and ordinal:
        return retrieval.apply_ordinal_ordering(
            scored,
            subject_entity_ids=context_entity_ids,
            subject_names=None,
            ordinal=ordinal,
            multi_subject=True,
        )
    return scored


async def apply_range_filter_if_eligible(
    *,
    config,
    retrieval,
    query: str,
    candidates: list[tuple[str, float]],
    query_range: object | None,
    temporal_ref: object | None,
    context_entity_ids: list[str],
    emit_stage: Callable,
) -> list[tuple[str, float]]:
    """Phase B.4 — hard-filter candidates by temporal range."""
    from ncms.domain.temporal.intent import (
        TemporalIntent,
        classify_temporal_intent,
    )
    from ncms.domain.temporal.normalizer import NormalizedInterval, RawSpan

    if not config.temporal_range_filter_enabled:
        return candidates
    if not candidates:
        return candidates

    ordinal = getattr(temporal_ref, "ordinal", None) if temporal_ref else None
    has_range = bool(temporal_ref) and bool(
        getattr(temporal_ref, "range_start", None) or getattr(temporal_ref, "range_end", None),
    )
    has_relative = bool(temporal_ref) and bool(
        getattr(temporal_ref, "recency_bias", False),
    )
    intent = classify_temporal_intent(
        query,
        ordinal=ordinal,
        has_range=has_range,
        has_relative=has_relative,
        subject_count=len(context_entity_ids),
    )
    if intent not in (TemporalIntent.RANGE, TemporalIntent.RELATIVE_ANCHOR):
        return candidates

    # Prefer parser range; fall back to normalizer-produced range.
    interval: NormalizedInterval | None = None
    if has_range:
        r_start = getattr(temporal_ref, "range_start", None)
        r_end = getattr(temporal_ref, "range_end", None)
        if r_start is not None and r_end is not None:
            interval = NormalizedInterval(
                start=r_start,
                end=r_end,
                confidence=0.9,
                source_span=RawSpan("<parser>", "date"),
                origin="parser",
            )
    if interval is None and isinstance(query_range, NormalizedInterval):
        interval = query_range
    if interval is None:
        return candidates

    before = len(candidates)
    filtered = await retrieval.apply_range_filter(
        candidates,
        interval,
        missing_range_policy=config.temporal_missing_range_policy,
    )
    emit_stage(
        "temporal_range_filtered",
        0.0,
        {
            "intent": intent.value,
            "candidates_before": before,
            "candidates_after": len(filtered),
            "policy": config.temporal_missing_range_policy,
            "range_source": interval.origin,
        },
    )
    return filtered


async def classify_search_intent(
    *,
    config,
    intent_classifier,
    query: str,
    intent_override: str | None,
    emit_stage: Callable,
) -> IntentResult | None:
    """Classify query intent via exemplar index, keyword fallback, or LLM."""
    if intent_override is not None:
        from ncms.domain.intent import INTENT_TARGETS

        try:
            qi = QueryIntent(intent_override)
        except ValueError:
            valid = [e.value for e in QueryIntent]
            raise ValueError(  # noqa: B904
                f"Invalid intent '{intent_override}'. Valid intents: {valid}"
            )
        emit_stage(
            "intent_override",
            0.0,
            {"intent": qi.value, "source": "user_override"},
        )
        return IntentResult(
            intent=qi,
            confidence=1.0,
            target_node_types=INTENT_TARGETS.get(qi, ("atomic",)),
        )

    if not config.temporal_enabled:
        return None

    t0 = time.perf_counter()
    if intent_classifier is not None:
        intent_result = intent_classifier.classify(query)
    else:
        intent_result = classify_intent(query)

    llm_fallback_used = False
    if intent_result.confidence < config.intent_confidence_threshold:
        if config.intent_llm_fallback_enabled:
            from ncms.infrastructure.llm.intent_classifier_llm import (
                classify_intent_with_llm,
            )

            llm_result = await classify_intent_with_llm(
                query,
                model=config.llm_model,
                api_base=config.llm_api_base,
            )
            if llm_result is not None:
                intent_result = llm_result
                llm_fallback_used = True
            else:
                emit_stage(
                    "intent_llm_miss",
                    0,
                    {
                        "query": query[:200],
                        "bm25_intent": intent_result.intent.value,
                        "bm25_confidence": round(intent_result.confidence, 3),
                    },
                )

        if intent_result.confidence < config.intent_confidence_threshold:
            emit_stage(
                "intent_miss",
                0,
                {
                    "query": query[:200],
                    "best_intent": intent_result.intent.value,
                    "best_confidence": round(intent_result.confidence, 3),
                    "llm_attempted": llm_fallback_used,
                },
            )
            intent_result = IntentResult(
                intent=QueryIntent.FACT_LOOKUP,
                confidence=1.0,
                target_node_types=("atomic", "entity_state"),
            )

    emit_stage(
        "intent_classification",
        (time.perf_counter() - t0) * 1000,
        {
            "intent": intent_result.intent.value,
            "confidence": round(intent_result.confidence, 3),
            "target_node_types": list(intent_result.target_node_types),
            "llm_fallback": llm_fallback_used,
        },
    )
    return intent_result
