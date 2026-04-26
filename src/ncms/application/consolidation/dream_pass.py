"""Phase 8 dream-cycle implementation — extracted from
:class:`ConsolidationService` so the orchestrator stays under the
A-grade MI bar.

**Design principle**: every collaborator is named in the signature
(``store``, ``graph``, ``config``).  No ``svc=...`` whole-service
injection — the parameter list documents each helper's dependency
surface and makes them independently testable.

The class keeps thin delegating methods (``learn_association_strengths``,
``run_dream_cycle``, etc.) that pass ``self._store / self._graph /
self._config / self._event_log`` through to these functions.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import math as _math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ncms.domain.models import AccessRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rehearsal
# ---------------------------------------------------------------------------


async def _compute_dream_signals(
    *,
    store,
    graph,
    config,
    memories: list,
    centrality: dict[str, float],
) -> list[dict[str, Any]]:
    """Compute 5 selection signals for each eligible memory."""
    candidates: list[dict[str, Any]] = []
    for memory in memories:
        access_ages = await store.get_access_times(memory.id)
        access_count = len(access_ages)
        if access_count < config.dream_min_access_count:
            continue
        entity_ids = graph.get_entity_ids_for_memory(memory.id)
        mem_centrality = 0.0
        if entity_ids and centrality:
            scores = [centrality.get(eid, 0.0) for eid in entity_ids]
            mem_centrality = sum(scores) / len(scores) if scores else 0.0
        last_access_age = min(access_ages) if access_ages else float("inf")
        staleness = last_access_age / 86400.0
        recency = last_access_age / 86400.0 if access_ages else 0.0
        candidates.append(
            {
                "memory": memory,
                "centrality": mem_centrality,
                "staleness": staleness,
                "importance": memory.importance,
                "access_count": float(access_count),
                "recency": recency,
            }
        )
    return candidates


def _rank_normalize_and_score(*, config, candidates: list[dict[str, Any]]) -> None:
    """Rank-normalize signals to [0,1] and compute weighted dream score."""
    for signal in ("centrality", "staleness", "importance", "access_count", "recency"):
        values = [c[signal] for c in candidates]
        min_val, max_val = min(values), max(values)
        range_val = max_val - min_val
        for c in candidates:
            c[f"{signal}_norm"] = (c[signal] - min_val) / range_val if range_val > 0 else 0.5

    for c in candidates:
        c["dream_score"] = (
            config.dream_rehearsal_weight_centrality * c["centrality_norm"]
            + config.dream_rehearsal_weight_staleness * c["staleness_norm"]
            + config.dream_rehearsal_weight_importance * c["importance_norm"]
            + config.dream_rehearsal_weight_access_count * c["access_count_norm"]
            + config.dream_rehearsal_weight_recency * c["recency_norm"]
        )


async def _inject_dream_accesses(*, store, selected: list[dict[str, Any]]) -> int:
    """Inject differential synthetic access records."""
    if not selected:
        return 0
    max_dream = selected[0]["dream_score"]
    min_dream = selected[-1]["dream_score"]
    dream_range = max_dream - min_dream
    rehearsed = 0
    for c in selected:
        memory = c["memory"]
        if dream_range > 0:
            normalized = (c["dream_score"] - min_dream) / dream_range
            n_accesses = 1 + int(normalized * 4)
        else:
            n_accesses = 1
        for _i in range(n_accesses):
            await store.log_access(
                AccessRecord(
                    memory_id=memory.id,
                    accessing_agent="dream_rehearsal",
                    query_context=(
                        f"dream_cycle:score={c['dream_score']:.3f}:accesses={n_accesses}"
                    ),
                )
            )
        rehearsed += 1
    return rehearsed


async def run_dream_rehearsal(*, store, graph, config) -> int:
    """Select important memories and inject synthetic access records."""
    if not config.dream_cycle_enabled:
        return 0
    if not graph:
        logger.warning("Graph not available, skipping dream rehearsal")
        return 0
    centrality = graph.pagerank()
    memories = await store.list_memories(limit=100000)
    if not memories:
        return 0
    candidates = await _compute_dream_signals(
        store=store, graph=graph, config=config, memories=memories, centrality=centrality
    )
    if not candidates:
        return 0
    _rank_normalize_and_score(config=config, candidates=candidates)
    candidates.sort(key=lambda c: c["dream_score"], reverse=True)
    n_rehearse = max(1, int(len(candidates) * config.dream_rehearsal_fraction))
    selected = candidates[:n_rehearse]
    rehearsed = await _inject_dream_accesses(store=store, selected=selected)
    logger.info(
        "Dream rehearsal: rehearsed %d/%d eligible memories (from %d total)",
        rehearsed,
        len(candidates),
        len(memories),
    )
    return rehearsed


# ---------------------------------------------------------------------------
# PMI association learning + graph bridging
# ---------------------------------------------------------------------------


def _bridge_pmi_to_graph(
    *,
    graph,
    pair_count: dict[tuple[str, str], int],
    total_searches: int,
    entity_count: dict[str, int],
) -> int:
    """Bridge dream PMI associations into NetworkX graph edge weights."""
    if not pair_count or total_searches == 0:
        return 0

    pmi_values: list[tuple[str, str, float]] = []
    max_pmi = 0.01
    for (e1, e2), co_count in pair_count.items():
        p_ab = co_count / total_searches
        p_a = entity_count.get(e1, 1) / total_searches
        p_b = entity_count.get(e2, 1) / total_searches
        if p_a > 0 and p_b > 0 and p_ab > 0:
            pmi = _math.log2(p_ab / (p_a * p_b))
            pmi = max(pmi, 0.0)
        else:
            pmi = 0.0
        if pmi > 0.01:
            pmi_values.append((e1, e2, pmi))
            if pmi > max_pmi:
                max_pmi = pmi

    if graph is None:
        return 0

    updated = 0
    for e1, e2, pmi in pmi_values:
        weight = max(0.01, pmi / max_pmi)
        existing_fwd = graph.get_edge_weight(e1, e2)
        existing_rev = graph.get_edge_weight(e2, e1)
        if existing_fwd > 0:
            blended = 0.3 * existing_fwd + 0.7 * weight
            graph.set_edge_weight(e1, e2, blended)
            updated += 1
        if existing_rev > 0:
            blended = 0.3 * existing_rev + 0.7 * weight
            graph.set_edge_weight(e2, e1, blended)
            updated += 1
        if existing_fwd == 0 and existing_rev == 0 and weight > 0.9:
            from ncms.domain.models import Relationship

            graph.add_relationship(
                Relationship(
                    source_entity_id=e1,
                    target_entity_id=e2,
                    type="learned_association",
                    source_memory_id="dream_pmi",
                )
            )
            graph.set_edge_weight(e1, e2, weight)
            updated += 1
    return updated


async def learn_association_strengths(*, store, graph, config) -> int:
    """PMI co-occurrence learning from search-result entity pairs."""
    if not config.dream_cycle_enabled:
        return 0
    last_run = await store.get_consolidation_value("last_association_learning")
    pairs = await store.get_search_access_pairs(since=last_run)
    if not pairs:
        return 0

    entity_count: dict[str, int] = defaultdict(int)
    pair_count: dict[tuple[str, str], int] = defaultdict(int)
    total_searches = 0
    for _query, returned_ids in pairs:
        if not returned_ids:
            continue
        total_searches += 1
        search_entities: set[str] = set()
        for memory_id in returned_ids:
            entity_ids = graph.get_entity_ids_for_memory(memory_id) if graph else set()
            search_entities.update(entity_ids)
        for eid in search_entities:
            entity_count[eid] += 1
        entity_list = sorted(search_entities)
        for i, e1 in enumerate(entity_list):
            for e2 in entity_list[i + 1 :]:
                pair_count[(e1, e2)] += 1

    if total_searches == 0 or not pair_count:
        return 0

    saved = 0
    for (e1, e2), co_count in pair_count.items():
        p_ab = co_count / total_searches
        p_a = entity_count[e1] / total_searches
        p_b = entity_count[e2] / total_searches
        if p_a > 0 and p_b > 0 and p_ab > 0:
            pmi = _math.log(p_ab / (p_a * p_b))
            pmi_clamped = max(0.0, min(10.0, pmi))
            strength = pmi_clamped / 10.0
            if strength > 0.01:
                await store.save_association_strength(e1, e2, strength)
                saved += 1

    await store.set_consolidation_value("last_association_learning", datetime.now(UTC).isoformat())
    logger.info("Association learning: saved %d pairs from %d searches", saved, total_searches)
    if saved > 0 and graph:
        bridged = _bridge_pmi_to_graph(
            graph=graph,
            pair_count=pair_count,
            total_searches=total_searches,
            entity_count=entity_count,
        )
        logger.info("PMI-to-graph bridge: updated %d edge weights", bridged)
    return saved


# ---------------------------------------------------------------------------
# Importance drift / query expansion / active forgetting
# ---------------------------------------------------------------------------


async def adjust_importance_drift(*, store, config) -> int:
    """Adjust memory importance based on access rate trends."""
    if not config.dream_cycle_enabled:
        return 0
    half_window = timedelta(days=config.dream_importance_drift_window_days / 2.0)
    window = timedelta(days=config.dream_importance_drift_window_days)
    drift_rate = config.dream_importance_drift_rate

    memories = await store.list_memories(limit=100000)
    adjusted = 0
    for memory in memories:
        access_ages = await store.get_access_times(memory.id)
        if len(access_ages) < 2:
            continue
        recent_count = sum(1 for age in access_ages if age < half_window.total_seconds())
        older_count = sum(
            1 for age in access_ages if half_window.total_seconds() <= age < window.total_seconds()
        )
        half_days = half_window.total_seconds() / 86400.0
        recent_rate = recent_count / half_days if half_days > 0 else 0
        older_rate = older_count / half_days if half_days > 0 else 0
        if recent_rate > older_rate * 1.5:
            delta = drift_rate
        elif older_rate > recent_rate * 1.5:
            delta = -drift_rate
        else:
            continue
        new_importance = max(0.0, min(10.0, memory.importance + delta))
        if abs(new_importance - memory.importance) > 0.001:
            memory.importance = new_importance
            await store.update_memory(memory)
            adjusted += 1
    logger.info("Importance drift: adjusted %d/%d memories", adjusted, len(memories))
    return adjusted


async def build_query_expansion_dict(*, store, graph, config) -> int:
    """Build a PMI-based query expansion dictionary."""
    if not config.dream_query_expansion_enabled:
        return 0
    min_pmi = config.dream_expansion_min_pmi
    max_terms = config.dream_expansion_max_terms
    strong = await store.get_strong_associations(min_strength=min_pmi, limit=100_000)
    if not strong:
        logger.info("Query expansion: no associations above min_pmi=%.2f", min_pmi)
        return 0

    raw_expansions: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e1, e2, strength in strong:
        raw_expansions[e1].append((e2, strength))
        raw_expansions[e2].append((e1, strength))
    if not raw_expansions:
        return 0

    entity_name_cache: dict[str, str] = {}
    if graph:
        all_eids = set(raw_expansions.keys())
        for candidates in raw_expansions.values():
            for eid, _ in candidates:
                all_eids.add(eid)
        for eid in all_eids:
            name = graph.get_entity_name(eid)
            if name:
                entity_name_cache[eid] = name

    expansion_dict: dict[str, list[str]] = {}
    total_pairs = 0
    for eid, candidates in raw_expansions.items():
        candidates.sort(key=lambda x: x[1], reverse=True)
        top_names = [
            entity_name_cache[c[0]] for c in candidates[:max_terms] if c[0] in entity_name_cache
        ]
        if top_names:
            expansion_dict[eid] = top_names
            total_pairs += len(top_names)

    await store.set_consolidation_value("query_expansion_dict", _json.dumps(expansion_dict))
    logger.info(
        "Query expansion dict: %d entities with %d total expansion terms",
        len(expansion_dict),
        total_pairs,
    )
    return total_pairs


async def _decay_superseded_memories(*, store, decay_rate: float, access_prune_days: int) -> int:
    from ncms.domain.models import NodeType

    affected = 0
    superseded_nodes = await store.get_memory_nodes_by_type(NodeType.ENTITY_STATE.value)
    for node in superseded_nodes:
        if node.is_current:
            continue
        memory = await store.get_memory(node.memory_id)
        if not memory:
            continue
        new_importance = max(0.0, memory.importance - decay_rate)
        if abs(new_importance - memory.importance) > 0.001:
            memory.importance = new_importance
            await store.update_memory(memory)
            affected += 1
        with contextlib.suppress(Exception):
            pruned = await store.prune_access_records(node.memory_id, access_prune_days)
            if pruned > 0:
                logger.debug(
                    "Pruned %d access records for superseded memory %s",
                    pruned,
                    node.memory_id,
                )
    return affected


async def _decay_conflicting_memories(*, store, decay_rate: float, conflict_age_days: int) -> int:
    from ncms.domain.models import EdgeType, NodeType

    affected = 0
    cutoff = (datetime.now(UTC) - timedelta(days=conflict_age_days)).isoformat()
    entity_state_nodes = await store.get_memory_nodes_by_type(NodeType.ENTITY_STATE.value)
    for node in entity_state_nodes:
        if not node.is_current:
            continue
        conflict_edges = await store.get_graph_edges(node.id, EdgeType.CONFLICTS_WITH)
        if not conflict_edges:
            continue
        if node.metadata.get("ingested_at", "") < cutoff:
            memory = await store.get_memory(node.memory_id)
            if memory:
                new_importance = max(0.0, memory.importance - decay_rate * 0.5)
                if abs(new_importance - memory.importance) > 0.001:
                    memory.importance = new_importance
                    await store.update_memory(memory)
                    affected += 1
    return affected


async def active_forgetting(*, store, config) -> int:
    """Suppress superseded/conflicting memories (Phase 9)."""
    if not config.dream_active_forgetting_enabled:
        return 0
    decay_rate = config.dream_forgetting_decay_rate
    access_prune_days = config.dream_forgetting_access_prune_days
    conflict_age_days = config.dream_forgetting_conflict_age_days

    affected = 0
    try:
        affected += await _decay_superseded_memories(
            store=store, decay_rate=decay_rate, access_prune_days=access_prune_days
        )
    except Exception:
        logger.debug("Active forgetting (supersession) failed", exc_info=True)
    try:
        affected += await _decay_conflicting_memories(
            store=store, decay_rate=decay_rate, conflict_age_days=conflict_age_days
        )
    except Exception:
        logger.debug("Active forgetting (conflicts) failed", exc_info=True)
    logger.info("Active forgetting: affected %d memories", affected)
    return affected


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_dream_cycle(*, store, graph, config, event_log) -> dict[str, int]:
    """Full dream cycle: rehearsal → assoc → expansion → forgetting → drift."""
    if not config.dream_cycle_enabled:
        return {"rehearsal": 0, "associations": 0, "drift": 0}

    results: dict[str, int] = {}
    with contextlib.suppress(Exception):
        results["rehearsal"] = await run_dream_rehearsal(store=store, graph=graph, config=config)
    with contextlib.suppress(Exception):
        results["associations"] = await learn_association_strengths(
            store=store, graph=graph, config=config
        )
    try:
        results["query_expansion"] = await build_query_expansion_dict(
            store=store, graph=graph, config=config
        )
    except Exception:
        logger.warning("Query expansion dict failed", exc_info=True)
    try:
        results["forgetting"] = await active_forgetting(store=store, config=config)
    except Exception:
        logger.warning("Active forgetting failed", exc_info=True)
    with contextlib.suppress(Exception):
        results["drift"] = await adjust_importance_drift(store=store, config=config)

    results.setdefault("rehearsal", 0)
    results.setdefault("associations", 0)
    results.setdefault("query_expansion", 0)
    results.setdefault("forgetting", 0)
    results.setdefault("drift", 0)

    if event_log is not None:
        with contextlib.suppress(Exception):
            event_log.dream_cycle_complete(results=results)
    logger.info("Dream cycle complete: %s", results)
    return results
