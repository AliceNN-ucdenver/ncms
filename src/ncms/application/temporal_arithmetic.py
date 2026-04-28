"""Helpers for :meth:`MemoryService.compute_temporal_arithmetic`.

Pure free functions that resolve anchor entities to representative
memories + dates, and pick one per anchor when the entity has multiple
candidates.  Extracted from ``memory_service.py`` so the orchestrator
stays under the B+ MI bar.

Public entry points:

* :func:`extract_anchor_entity_names`
* :func:`resolve_anchor_dates`

The other helpers are private to this module.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ncms.domain.models import Memory

logger = logging.getLogger(__name__)


async def extract_anchor_entity_names(
    *,
    store,
    config,
    query: str,
    domain: str | None,
) -> list[str]:
    """Configured query entity extraction → subject names."""
    from ncms.application.entity_extraction_mode import use_gliner_entities

    if not use_gliner_entities(config):
        return []

    from ncms.application.label_cache import load_cached_labels
    from ncms.application.retrieval.pipeline import RetrievalPipeline
    from ncms.domain.entity_extraction import (
        add_temporal_labels,
        resolve_labels,
    )
    from ncms.infrastructure.extraction.gliner_extractor import (
        extract_with_label_budget,
    )

    search_domains = [domain] if domain else []
    cached = await load_cached_labels(store, search_domains)
    labels = resolve_labels(search_domains, cached_labels=cached)
    if config.temporal_range_filter_enabled:
        labels = add_temporal_labels(labels)
    mixed = extract_with_label_budget(
        query,
        labels,
        model_name=config.gliner_model,
        threshold=config.gliner_threshold,
        cache_dir=config.model_cache_dir,
    )
    entities, _temporal = RetrievalPipeline.split_entity_and_temporal_spans(mixed)
    return [str(e.get("name", "")) for e in entities if e.get("name")]


async def _bm25_anchor_ranking(
    *,
    index,
    config,
    query: str,
) -> dict[str, float]:
    """Return ``{memory_id: bm25_score}`` for the query."""
    try:
        ranked = await asyncio.to_thread(index.search, query, config.tier1_candidates)
    except Exception:
        return {}
    return {mid: score for mid, score in ranked}


def _pick_anchor_memory(
    *,
    candidates: list,
    bm25_scores: dict[str, float],
) -> tuple[Memory, datetime] | None:
    """BM25-top preferred; earliest-by-date fallback."""
    if bm25_scores:
        ranked = sorted(
            (
                (mem, bm25_scores.get(mem.id, -1.0))
                for mem in candidates
                if bm25_scores.get(mem.id, -1.0) >= 0.0
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )
        if ranked:
            top_mem = ranked[0][0]
            when = getattr(top_mem, "observed_at", None) or getattr(top_mem, "created_at", None)
            if when is not None:
                return top_mem, when
    # Fallback: earliest observed_at.
    best: tuple[Memory, datetime] | None = None
    for mem in candidates:
        when = getattr(mem, "observed_at", None) or getattr(mem, "created_at", None)
        if when is None:
            continue
        if best is None or when < best[1]:
            best = (mem, when)
    return best


async def _candidates_for_anchor(
    *,
    store,
    graph,
    name: str,
) -> list:
    """Gather memories for a given anchor name via graph + text scan."""
    seen_ids: set[str] = set()
    candidates: list = []
    eid = graph.find_entity_by_name(name)
    if eid is None:
        ent = await store.find_entity_by_name(name)
        if ent is not None:
            eid = ent.id
    if eid is not None:
        linked_ids = graph.get_memory_ids_for_entity(eid)
        if linked_ids:
            batch = await store.get_memories_batch(list(linked_ids))
            for mid, mem in batch.items():
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    candidates.append(mem)
    if len(name.strip()) >= 3:
        needle = name.strip().lower()
        try:
            all_mems = await store.list_memories()
        except Exception:
            all_mems = []
        for mem in all_mems:
            if mem.id in seen_ids:
                continue
            if needle in (mem.content or "").lower():
                seen_ids.add(mem.id)
                candidates.append(mem)
    return candidates


async def resolve_anchor_dates(
    *,
    store,
    graph,
    index,
    config,
    anchor_names: list[str],
    query: str | None = None,
) -> tuple[list[datetime], list[Memory]]:
    """Resolve each anchor name to a representative memory + event date.

    See :meth:`MemoryService.compute_temporal_arithmetic` for the
    full picking strategy.
    """
    bm25_ranking = (
        await _bm25_anchor_ranking(index=index, config=config, query=query) if query else {}
    )
    dates: list[datetime] = []
    memories: list[Memory] = []
    for name in anchor_names:
        candidates = await _candidates_for_anchor(store=store, graph=graph, name=name)
        if not candidates:
            continue
        picked = _pick_anchor_memory(candidates=candidates, bm25_scores=bm25_ranking)
        if picked is None:
            continue
        memories.append(picked[0])
        dates.append(picked[1])
    return dates, memories
