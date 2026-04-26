"""Subject-scoped zone walkers used by :func:`retrieve_lg`.

Pure functions: each walker takes the subject's zone structure
(``node_index``, ``zone_edges``) plus the parsed :class:`LGTrace`,
mutates the trace in-place, and returns ``None``.  Confidence
levels (HIGH / MEDIUM / ABSTAIN) follow the composition invariant
documented in :mod:`ncms.application.tlg.dispatch`.

Extracted from ``dispatch.py`` in the Phase F MI cleanup so the
orchestrator (``retrieve_lg`` + intent-routing layer) stays under
the A-grade maintainability bar.
"""

from __future__ import annotations

import logging
import re as _re
from datetime import datetime
from datetime import timedelta as _timedelta
from typing import TYPE_CHECKING, Any

from ncms.domain.models import MemoryNode
from ncms.domain.tlg import Confidence, LGTrace

if TYPE_CHECKING:
    from ncms.application.tlg.dispatch import _DispatchCtx  # noqa: F401

logger = logging.getLogger(__name__)


# ± 7-day window for concurrent-intent in-subject approximation.
_CONCURRENT_WINDOW = _timedelta(days=7)


async def _find_event_node(
    store: object,
    subject: str,
    event_name: str,
    node_index: dict[str, MemoryNode],
) -> MemoryNode | None:
    """Resolve an entity-name phrase (e.g. ``"session cookies"``) to
    the earliest subject-scoped ENTITY_STATE node that mentions it.

    Phase 4 O(1) entity index: first ask the store for the memory IDs
    that link to the entity (SQL index lookup, not a node scan).
    Filter the result down to this subject's ENTITY_STATE nodes and
    return the earliest.

    Falls back to a full node scan (three-tier match: exact entity
    equality → entity substring → content word-boundary) when the
    index returns nothing — catches entities that were spelled
    differently from any canonical entity record, matching the
    research ``_find_memory`` behaviour.
    """
    if not event_name:
        return None
    needle = event_name.strip().lower()
    if not needle:
        return None

    memory_to_node: dict[str, MemoryNode] = {n.memory_id: n for n in node_index.values()}

    # Fast path — O(log N) store index lookup.
    try:
        candidate_memory_ids = await store.find_memory_ids_by_entity(needle)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — defensive guard
        candidate_memory_ids = []
    indexed_nodes = [memory_to_node[mid] for mid in candidate_memory_ids if mid in memory_to_node]
    if indexed_nodes:
        indexed_nodes.sort(key=lambda n: n.observed_at or n.created_at)
        return indexed_nodes[0]

    # Fallback — three-tier scan over subject nodes for the edge
    # cases where the entity isn't registered under the queried name.
    nodes = list(node_index.values())
    nodes.sort(key=lambda n: n.observed_at or n.created_at)

    pattern = _re.compile(r"\b" + _re.escape(needle) + r"\b", _re.IGNORECASE)
    for node in nodes:
        try:
            entities = await store.get_memory_entities(node.memory_id)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover — defensive guard
            continue
        ents_low = [e.lower() for e in entities]
        if needle in ents_low or any(needle in e for e in ents_low):
            return node
        try:
            memory = await store.get_memory(node.memory_id)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover — defensive guard
            memory = None
        if memory is not None and memory.content and pattern.search(memory.content):
            return node
    return None


# ---------------------------------------------------------------------------
# New-intent dispatchers — sequence / predecessor / interval / range /
#                         concurrent / before_named / transitive_cause /
#                         cause_of
# ---------------------------------------------------------------------------


async def _dispatch_sequence(
    store: object,
    trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
    zone_edges: list,
) -> None:
    """``what came after X`` — direct chain successor of X."""
    subject = trace.intent.subject
    entity = trace.intent.entity
    if subject is None or not entity:
        trace.proof = "sequence: missing subject or entity"
        trace.confidence = Confidence.ABSTAIN
        return
    x_node = await _find_event_node(store, subject, entity, node_index)
    if x_node is None:
        trace.proof = f"sequence: could not resolve {entity!r} in subject"
        trace.confidence = Confidence.ABSTAIN
        return
    # ZoneEdge.src/dst is inverted to old→new in _load_subject_zones,
    # so the successor of X is the edge whose src == X.id.
    successor = next(
        (e for e in zone_edges if e.src == x_node.id),
        None,
    )
    if successor is None:
        trace.proof = f"sequence: no admissible successor edge from {x_node.id}"
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = successor.dst
    trace.proof = (
        f"sequence(subject={subject}, after={entity}@{x_node.id}): "
        f"successor = {successor.dst} via {successor.transition}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_predecessor(
    store: object,
    trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
    zone_edges: list,
) -> None:
    """``what came before X`` — direct chain predecessor of X."""
    subject = trace.intent.subject
    entity = trace.intent.entity
    if subject is None or not entity:
        trace.proof = "predecessor: missing subject or entity"
        trace.confidence = Confidence.ABSTAIN
        return
    x_node = await _find_event_node(store, subject, entity, node_index)
    if x_node is None:
        trace.proof = f"predecessor: could not resolve {entity!r}"
        trace.confidence = Confidence.ABSTAIN
        return
    # Predecessor = edge whose dst == X.id in the zone-direction graph.
    predecessor = next(
        (e for e in zone_edges if e.dst == x_node.id),
        None,
    )
    if predecessor is None:
        trace.proof = f"predecessor: no admissible predecessor edge into {x_node.id}"
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = predecessor.src
    trace.proof = (
        f"predecessor(subject={subject}, before={entity}@{x_node.id}): "
        f"predecessor = {predecessor.src}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_interval(
    store: object,
    trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
) -> None:
    """``between X and Y`` — memories in subject with observed_at
    strictly between X's and Y's observed_at, earliest ranked first."""
    subject = trace.intent.subject
    x_name = trace.intent.entity
    y_name = trace.intent.secondary
    if subject is None or not x_name or not y_name:
        trace.proof = "interval: missing subject, X, or Y"
        trace.confidence = Confidence.ABSTAIN
        return
    x_node = await _find_event_node(store, subject, x_name, node_index)
    y_node = await _find_event_node(store, subject, y_name, node_index)
    if x_node is None or y_node is None:
        trace.proof = "interval: could not resolve X or Y"
        trace.confidence = Confidence.ABSTAIN
        return
    x_t = x_node.observed_at or x_node.created_at
    y_t = y_node.observed_at or y_node.created_at
    lo, hi = min(x_t, y_t), max(x_t, y_t)
    between = [n for n in node_index.values() if lo < (n.observed_at or n.created_at) < hi]
    if not between:
        trace.proof = f"interval(subject={subject}, [{x_name}, {y_name}]): empty"
        trace.confidence = Confidence.ABSTAIN
        return
    between.sort(key=lambda n: n.observed_at or n.created_at)
    trace.grammar_answer = between[0].id
    trace.zone_context = [n.id for n in between[1:]]
    trace.proof = (
        f"interval(subject={subject}): {len(between)} memories between {x_name} and {y_name}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_range(
    store: object,
    trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
    range_start: str | None,
    range_end: str | None,
) -> None:
    """Calendar range — memories in subject with observed_at in
    [range_start, range_end), chronological order."""
    subject = trace.intent.subject
    if subject is None or not range_start or not range_end:
        trace.proof = "range: missing subject or bounds"
        trace.confidence = Confidence.ABSTAIN
        return
    try:
        rs = datetime.fromisoformat(range_start)
        re_ = datetime.fromisoformat(range_end)
    except ValueError:
        trace.proof = "range: malformed bounds"
        trace.confidence = Confidence.ABSTAIN
        return
    hits = [
        n for n in node_index.values() if n.observed_at is not None and rs <= n.observed_at < re_
    ]
    if not hits:
        trace.proof = f"range(subject={subject}, [{range_start[:10]}, {range_end[:10]})): empty"
        trace.confidence = Confidence.ABSTAIN
        return
    hits.sort(key=lambda n: n.observed_at or n.created_at)
    trace.grammar_answer = hits[0].id
    trace.zone_context = [n.id for n in hits[1:]]
    trace.proof = f"range(subject={subject}): {len(hits)} memories in window"
    trace.confidence = Confidence.HIGH


async def _dispatch_before_named(
    store: object,
    trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
) -> None:
    """``did X come before Y`` — compare observed_at across X and Y."""
    subject = trace.intent.subject
    x_name = trace.intent.entity
    y_name = trace.intent.secondary
    if subject is None or not x_name or not y_name:
        trace.proof = "before_named: missing slots"
        trace.confidence = Confidence.ABSTAIN
        return
    x_node = await _find_event_node(store, subject, x_name, node_index)
    y_node = await _find_event_node(store, subject, y_name, node_index)
    if x_node is None or y_node is None:
        trace.proof = "before_named: could not resolve X or Y"
        trace.confidence = Confidence.ABSTAIN
        return
    x_t = x_node.observed_at or x_node.created_at
    y_t = y_node.observed_at or y_node.created_at
    earlier = x_node if x_t <= y_t else y_node
    later = y_node if earlier is x_node else x_node
    trace.grammar_answer = earlier.id
    trace.zone_context = [later.id]
    trace.proof = f"before_named(subject={subject}): earlier={earlier.id}, later={later.id}"
    trace.confidence = Confidence.HIGH


async def _walk_causal_chain(
    start_memory_id: str,
    causal_edges: list,
    *,
    max_depth: int = 6,
) -> tuple[list[str], list[str]]:
    """Walk CAUSED_BY + ENABLES edges backward from an effect node.

    Returns ``(path, edge_types)`` where:
      * ``path[0]`` is ``start_memory_id`` (the effect), and
        successive elements are that node's direct/transitive causes
        (following the src→dst direction of CAUSED_BY).
      * ``edge_types[i]`` is the edge type used to step from
        ``path[i]`` to ``path[i+1]``.

    BFS-style traversal picking the highest-confidence outgoing edge
    at each step.  Stops at ``max_depth`` OR when a cycle is detected
    OR when the current node has no outgoing causal edges.
    """
    # index: src → list of outgoing causal edges (effect→cause direction)
    out_edges: dict[str, list] = {}
    for e in causal_edges:
        out_edges.setdefault(e.src, []).append(e)

    path = [start_memory_id]
    edge_types: list[str] = []
    visited = {start_memory_id}
    cur = start_memory_id
    while len(path) - 1 < max_depth:
        outs = out_edges.get(cur, [])
        if not outs:
            break
        # Pick the highest-confidence edge.
        best = max(outs, key=lambda e: getattr(e, "confidence", 1.0))
        nxt = best.dst
        if nxt in visited:
            break
        path.append(nxt)
        edge_types.append(best.edge_type)
        visited.add(nxt)
        cur = nxt
    return path, edge_types


async def _dispatch_transitive_cause(
    store: object,
    trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
    zone_edges: list,
    ctx: _DispatchCtx | None = None,
) -> None:
    """``what eventually led to X`` — walk causal chain backward.

    **CTLG v8+ path** (when CAUSED_BY edges exist in the store): walks
    the typed causal graph via ``G_tr,c``, ranks candidate trajectories
    by h_explanatory + h_parsimony, returns the highest-scoring
    ancestor.  Chain traces come from the :func:`_walk_causal_chain`
    helper; ranking uses the default ``chain_cause_of`` weights from
    :mod:`ncms.domain.tlg.heuristics`.

    **Pre-CTLG fallback**: if no CAUSED_BY edges are loaded (pre-v8
    adapter, no ingest-time cue tagger, etc.), the walker drops to
    the original timestamp-predecessor logic so v7.x deployments
    continue to work unchanged.
    """
    from ncms.domain.tlg.heuristics import (
        HeuristicContext,
        Trajectory,
        rank_trajectories,
        score_trajectory,
        weights_for_relation,
    )

    subject = trace.intent.subject
    entity = trace.intent.entity
    if subject is None or not entity:
        trace.proof = "transitive_cause: missing slots"
        trace.confidence = Confidence.ABSTAIN
        return
    x_node = await _find_event_node(store, subject, entity, node_index)
    if x_node is None:
        trace.proof = f"transitive_cause: could not resolve {entity!r}"
        trace.confidence = Confidence.ABSTAIN
        return

    # ── CTLG v8+ path: walk CAUSED_BY graph ────────────────────────
    causal_edges: list = []
    if ctx is not None:
        try:
            causal_edges = await ctx.get_causal_edges()
        except Exception:
            logger.debug(
                "[tlg] causal graph load failed — falling to v7 path",
                exc_info=True,
            )
            causal_edges = []

    if causal_edges:
        path, edge_types = await _walk_causal_chain(
            x_node.memory_id,
            causal_edges,
            max_depth=6,
        )
        if len(path) > 1:
            # Build a typed Trajectory so future walkers can pick
            # among competing chains consistently.
            traj = Trajectory(
                kind="causal_chain",
                memory_ids=tuple(path),
                edge_types=tuple(edge_types),
                subject=subject,
                # Robustness + explanatory fields are 0 here — the
                # walker has no state-key coverage info; ranking
                # collapses to h_parsimony which is fine for chain
                # selection.  Enriched by the composition at §7.3.
            )
            h_ctx = HeuristicContext(
                total_state_keys=1,
                min_length=2,
                parsimony_alpha=0.2,
            )
            scored = score_trajectory(
                traj,
                h_ctx,
                heuristics=["h_parsimony", "h_explanatory"],
            )
            weights = weights_for_relation("chain_cause_of")
            ranked = rank_trajectories([scored], weights, context=h_ctx)
            winner = ranked[0]
            ancestor = winner.memory_ids[-1]
            trace.grammar_answer = ancestor
            trace.zone_context = list(winner.memory_ids[1:-1])
            trace.proof = (
                f"transitive_cause(CTLG causal chain, subject={subject}, "
                f"for={entity}): ancestor={ancestor} depth={len(path) - 1} "
                f"edge_types={edge_types} h={winner.heuristic_scores}"
            )
            trace.confidence = Confidence.HIGH
            return

    # ── Fallback: pre-CTLG timestamp-predecessor walk ──────────────
    # Walk dst→src via zone_edges until no predecessor remains.
    by_dst: dict[str, Any] = {e.dst: e for e in zone_edges}
    fallback_path: list[str] = [x_node.id]
    visited = {x_node.id}
    cur = x_node.id
    while cur in by_dst:
        edge = by_dst[cur]
        src = edge.src
        if src in visited:
            break
        visited.add(src)
        fallback_path.append(src)
        cur = src
    ancestor = fallback_path[-1]
    if ancestor == x_node.id:
        trace.proof = f"transitive_cause: no ancestors for {x_node.id}"
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = ancestor
    trace.zone_context = fallback_path[1:-1]
    trace.proof = (
        f"transitive_cause(pre-CTLG predecessor chain, subject={subject}, "
        f"for={entity}): earliest ancestor={ancestor} "
        f"(walked {len(fallback_path) - 1} predecessors)"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_concurrent(
    store: object,
    trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
) -> None:
    """``what was happening during X`` — cross-subject memories whose
    observed_at window overlaps X.  ABSTAINS when X can't be resolved
    or when no other memories exist in the overlap window — we don't
    try to synthesize a cross-subject fan-out here (that would need a
    global memory index); Phase 4 can extend if needed.
    """
    subject = trace.intent.subject
    entity = trace.intent.entity
    if subject is None or not entity:
        trace.proof = "concurrent: missing slots"
        trace.confidence = Confidence.ABSTAIN
        return
    x_node = await _find_event_node(store, subject, entity, node_index)
    if x_node is None:
        trace.proof = f"concurrent: could not resolve {entity!r}"
        trace.confidence = Confidence.ABSTAIN
        return
    # For a pure in-subject answer: find subject memories whose
    # observed_at window overlaps X's.  Phase 4 can widen to all
    # subjects via a global ENTITY_STATE scan.
    x_t = x_node.observed_at or x_node.created_at
    window_start = x_t - _CONCURRENT_WINDOW
    window_end = x_t + _CONCURRENT_WINDOW
    overlap = [
        n
        for n in node_index.values()
        if n.id != x_node.id and window_start <= (n.observed_at or n.created_at) <= window_end
    ]
    if not overlap:
        trace.proof = f"concurrent(subject={subject}, for={entity}): no overlapping memories"
        trace.confidence = Confidence.ABSTAIN
        return
    overlap.sort(key=lambda n: n.observed_at or n.created_at)
    trace.grammar_answer = overlap[0].id
    trace.zone_context = [n.id for n in overlap[1:]]
    trace.proof = (
        f"concurrent(subject={subject}): {len(overlap)} subject memories overlapping {entity}"
    )
    trace.confidence = Confidence.MEDIUM  # in-subject only is an approx.


async def _dispatch_cause_of(
    store: object,
    trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
) -> None:
    """``what caused X`` — resolve X to the first memory that mentions
    it in the subject, return that memory (the causal anchor).

    Low-confidence path: without the research's full content-marker
    fallback this is an approximate answer.  Assign MEDIUM when the
    entity resolves directly; ABSTAIN otherwise.  BM25 retains
    control when we abstain.
    """
    subject = trace.intent.subject
    entity = trace.intent.entity
    if subject is None or not entity:
        trace.proof = "cause_of: missing slots"
        trace.confidence = Confidence.ABSTAIN
        return
    x_node = await _find_event_node(store, subject, entity, node_index)
    if x_node is None:
        trace.proof = f"cause_of: could not resolve {entity!r}"
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = x_node.id
    trace.proof = (
        f"cause_of(subject={subject}, for={entity}): "
        f"earliest memory mentioning {entity} = {x_node.id}"
    )
    trace.confidence = Confidence.MEDIUM


def _any_entity_covers_needle(
    linked: list[str],
    needles: set[str],
) -> bool:
    """Substring / prefix-aware match for the still-intent heuristic.

    An entity "covers" a needle when:

    * it equals the needle exactly (lowercase),
    * it contains the needle as a token-prefix, or
    * the needle contains the entity as a substring (handles the
      inverse case where the parser captured a longer phrase than
      the registered entity).
    """
    for raw in linked:
        e = raw.lower()
        for n in needles:
            if e == n:
                return True
            if e.startswith(n + " ") or e.endswith(" " + n):
                return True
            if f" {n} " in f" {e} ":
                return True
            # Inverse — needle contains entity (parser extracted a
            # longer phrase like "oauth 2" while entity is "oauth").
            if e and (e in n or n in e):
                return True
    return False
