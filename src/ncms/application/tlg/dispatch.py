"""Query-side grammar dispatch (``retrieve_lg``).

Runs the L3 structural parser (:func:`analyze_query`) against the
L1 vocabulary + L2 markers + alias inventory, then routes the
parsed :class:`QueryStructure` through an intent-specific
dispatcher.  Every dispatcher walks the subject's zone graph
(:func:`_load_subject_zones`) and returns an :class:`LGTrace`.

Supported intents (Phase 3d — full research-parity coverage):

* ``current``          — terminal of the current zone (HIGH).
* ``origin``           — root of the earliest zone (HIGH).
* ``still``            — structural retirement lookup +
                         entity-in-current-zone fallback (HIGH / MEDIUM).
* ``retirement``       — same lookup surface as still (HIGH).
* ``sequence``         — admissible chain successor of X (HIGH).
* ``predecessor``      — admissible chain predecessor of X (HIGH).
* ``interval``         — observed_at strictly between X and Y (HIGH).
* ``range``            — observed_at in a calendar window (HIGH).
* ``before_named``     — two-event ordering compare (HIGH).
* ``transitive_cause`` — ancestor walk to earliest root (HIGH).
* ``concurrent``       — subject-scoped observed_at overlap (MEDIUM;
                         Phase 4 may widen cross-subject).
* ``cause_of``         — earliest memory mentioning X (MEDIUM;
                         content-marker fallback retired).

The composition invariant (``grammar ∨ BM25``) is enforced by the
Confidence level each dispatcher assigns: only HIGH / MEDIUM paths
are ever prepended onto the BM25 ranking.  ABSTAIN falls through.

See ``docs/p1-plan.md`` and ``docs/temporal-linguistic-geometry.md``
for the theory and phase context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ncms.domain.models import EdgeType, MemoryNode
from ncms.domain.tlg import (
    Confidence,
    LGIntent,
    LGTrace,
    QueryStructure,
    analyze_query,
    current_zone,
    origin_memory,
    retirement_memory,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subject-scoped helpers — built on top of ``ncms.domain.tlg.zones``.
# ---------------------------------------------------------------------------


# NCMS EdgeType → zones transition-name mapping.  We only consider
# edges that participate in the zone grammar; SUPPORTS / DERIVED_FROM
# / MENTIONS_ENTITY don't appear here.
_TRANSITION_FOR_EDGE: dict[str, str] = {
    EdgeType.SUPERSEDES.value: "supersedes",
    EdgeType.REFINES.value: "refines",
}


async def _load_subject_zones(
    store: object, subject: str,
) -> tuple[list, dict[str, MemoryNode], list]:
    """Load the zone structure + supporting indexes for ``subject``.

    Returns ``(zones, node_index, zone_edges)``:

    * ``zones`` — list of :class:`~ncms.domain.tlg.Zone` in
      chronological root order.
    * ``node_index`` — ``node.id → MemoryNode`` for the subject's
      ENTITY_STATE nodes.
    * ``zone_edges`` — the :class:`~ncms.domain.tlg.ZoneEdge` list,
      kept for :func:`retirement_memory` callers.

    Pure read path — no writes.  Safe to call repeatedly; if the
    caller wants caching they should memoise at a higher layer.
    """
    from ncms.domain.tlg import ZoneEdge as _ZoneEdge
    from ncms.domain.tlg import compute_zones as _compute_zones

    nodes = await store.get_entity_states_by_entity(subject)  # type: ignore[attr-defined]
    node_index = {n.id: n for n in nodes}
    node_ids = set(node_index.keys())

    # Direction inversion: NCMS reconciliation stores SUPERSEDES /
    # REFINES as source=new, target=existing.  The research zone
    # walker expects src=old, dst=new so the state flows forward in
    # time along admissible edges.  We invert on the way in.
    zone_edges: list = []
    for node in nodes:
        edges = await store.get_graph_edges(node.id)  # type: ignore[attr-defined]
        for edge in edges:
            transition = _TRANSITION_FOR_EDGE.get(edge.edge_type.value)
            if transition is None:
                continue
            # ``edge.source_id`` is the announcer (new); ``target_id``
            # is the existing (old).  In the zone model we want
            # ``src=old, dst=new``, so swap.
            zone_src = edge.target_id
            zone_dst = edge.source_id
            if zone_src not in node_ids or zone_dst not in node_ids:
                continue
            zone_edges.append(_ZoneEdge(
                src=zone_src,
                dst=zone_dst,
                transition=transition,
                retires_entities=frozenset(edge.retires_entities),
            ))

    zones = _compute_zones(subject, list(nodes), zone_edges)
    return zones, node_index, zone_edges


# ---------------------------------------------------------------------------
# Intent dispatch
# ---------------------------------------------------------------------------


# Stale per-intent dispatcher helpers (``_dispatch_current`` /
# ``_dispatch_origin`` / ``_dispatch_still``) were removed in the
# Phase 3d port — their zone-walker logic now lives inline in
# :func:`_route_intent` alongside every other production.  Keeping a
# single switch-point makes it easy to maintain the
# confidence-invariant contract across all 12 intents.


# ---------------------------------------------------------------------------
# Event-name → MemoryNode resolver
# ---------------------------------------------------------------------------


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

    memory_to_node: dict[str, MemoryNode] = {
        n.memory_id: n for n in node_index.values()
    }

    # Fast path — O(log N) store index lookup.
    try:
        candidate_memory_ids = await store.find_memory_ids_by_entity(needle)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — defensive guard
        candidate_memory_ids = []
    indexed_nodes = [
        memory_to_node[mid]
        for mid in candidate_memory_ids
        if mid in memory_to_node
    ]
    if indexed_nodes:
        indexed_nodes.sort(key=lambda n: (n.observed_at or n.created_at))
        return indexed_nodes[0]

    # Fallback — three-tier scan over subject nodes for the edge
    # cases where the entity isn't registered under the queried name.
    nodes = list(node_index.values())
    nodes.sort(key=lambda n: (n.observed_at or n.created_at))
    import re as _re
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
    store: object, trace: LGTrace,
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
        (e for e in zone_edges if e.src == x_node.id), None,
    )
    if successor is None:
        trace.proof = (
            f"sequence: no admissible successor edge from {x_node.id}"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = successor.dst
    trace.proof = (
        f"sequence(subject={subject}, after={entity}@{x_node.id}): "
        f"successor = {successor.dst} via {successor.transition}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_predecessor(
    store: object, trace: LGTrace,
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
        (e for e in zone_edges if e.dst == x_node.id), None,
    )
    if predecessor is None:
        trace.proof = (
            f"predecessor: no admissible predecessor edge into {x_node.id}"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = predecessor.src
    trace.proof = (
        f"predecessor(subject={subject}, before={entity}@{x_node.id}): "
        f"predecessor = {predecessor.src}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_interval(
    store: object, trace: LGTrace,
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
    between = [
        n for n in node_index.values()
        if lo < (n.observed_at or n.created_at) < hi
    ]
    if not between:
        trace.proof = (
            f"interval(subject={subject}, [{x_name}, {y_name}]): empty"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    between.sort(key=lambda n: (n.observed_at or n.created_at))
    trace.grammar_answer = between[0].id
    trace.zone_context = [n.id for n in between[1:]]
    trace.proof = (
        f"interval(subject={subject}): {len(between)} memories between "
        f"{x_name} and {y_name}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_range(
    store: object, trace: LGTrace,
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
        n for n in node_index.values()
        if n.observed_at is not None and rs <= n.observed_at < re_
    ]
    if not hits:
        trace.proof = (
            f"range(subject={subject}, "
            f"[{range_start[:10]}, {range_end[:10]})): empty"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    hits.sort(key=lambda n: n.observed_at or n.created_at)
    trace.grammar_answer = hits[0].id
    trace.zone_context = [n.id for n in hits[1:]]
    trace.proof = (
        f"range(subject={subject}): {len(hits)} memories in window"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_before_named(
    store: object, trace: LGTrace,
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
    trace.proof = (
        f"before_named(subject={subject}): "
        f"earlier={earlier.id}, later={later.id}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_transitive_cause(
    store: object, trace: LGTrace,
    *,
    node_index: dict[str, MemoryNode],
    zone_edges: list,
) -> None:
    """``what eventually led to X`` — walk admissible predecessors to
    the earliest ancestor."""
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
    # Walk dst→src via zone_edges until no predecessor remains.
    by_dst: dict[str, object] = {e.dst: e for e in zone_edges}
    path: list[str] = [x_node.id]
    visited = {x_node.id}
    cur = x_node.id
    while cur in by_dst:
        edge = by_dst[cur]
        src = edge.src  # type: ignore[union-attr]
        if src in visited:
            break
        visited.add(src)
        path.append(src)
        cur = src
    ancestor = path[-1]
    if ancestor == x_node.id:
        trace.proof = f"transitive_cause: no ancestors for {x_node.id}"
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = ancestor
    trace.zone_context = path[1:-1]  # middle of the chain
    trace.proof = (
        f"transitive_cause(subject={subject}, for={entity}): "
        f"earliest ancestor = {ancestor} "
        f"(walked {len(path)-1} predecessors)"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_concurrent(
    store: object, trace: LGTrace,
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
        n for n in node_index.values()
        if n.id != x_node.id
        and window_start <= (n.observed_at or n.created_at) <= window_end
    ]
    if not overlap:
        trace.proof = (
            f"concurrent(subject={subject}, for={entity}): "
            "no overlapping memories"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    overlap.sort(key=lambda n: (n.observed_at or n.created_at))
    trace.grammar_answer = overlap[0].id
    trace.zone_context = [n.id for n in overlap[1:]]
    trace.proof = (
        f"concurrent(subject={subject}): "
        f"{len(overlap)} subject memories overlapping {entity}"
    )
    trace.confidence = Confidence.MEDIUM  # in-subject only is an approx.


async def _dispatch_cause_of(
    store: object, trace: LGTrace,
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


# ± 7-day window for concurrent-intent in-subject approximation.
from datetime import timedelta as _timedelta  # noqa: E402

_CONCURRENT_WINDOW = _timedelta(days=7)


def _any_entity_covers_needle(
    linked: list[str], needles: set[str],
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def _load_induced_markers(store: object):
    """Pull the persisted L2 marker inventory for the parser.

    ``grammar_transition_markers`` is populated by
    :func:`ncms.application.tlg.induce_and_persist_markers`.  Empty
    dict on cold stores — parser still works with seed-only retirement
    vocabulary.
    """
    from ncms.domain.tlg import InducedEdgeMarkers
    try:
        persisted = await store.load_transition_markers()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — defensive guard
        persisted = {}
    return InducedEdgeMarkers(markers=persisted or {})


def _intent_to_lg_intent(qs: QueryStructure) -> LGIntent:
    return LGIntent(
        kind=qs.intent,
        subject=qs.subject,
        entity=qs.target_entity,
        secondary=qs.secondary_entity,
    )


async def retrieve_lg(
    query: str,
    *,
    store: object,
    vocabulary_cache: object,
    shape_cache: object | None = None,
) -> LGTrace:
    """Classify + dispatch a query against the grammar layer.

    Uses the L3 structural parser (``analyze_query``) for intent +
    slot filling, then routes through an intent-specific dispatcher
    that walks the subject's zone structure.  Returns an
    :class:`LGTrace`; never raises on missing data.

    When ``shape_cache`` is provided, skeleton matches short-circuit
    the production list with the cached intent (slots still refilled
    from the actual query).  Successful parses are memoised
    (persistently, via the shape-cache store) for future hits.

    Supported intents (Phase 3d):

    * ``current`` / ``origin`` / ``still`` — zone-walker based.
    * ``sequence`` / ``predecessor`` — chain neighbour lookup.
    * ``interval`` / ``range`` — observed_at window filter.
    * ``before_named`` — two-event ordering.
    * ``transitive_cause`` — ancestor walk.
    * ``concurrent`` — subject-scoped observed_at overlap (MEDIUM;
      Phase 4 may widen cross-subject).
    * ``cause_of`` / ``retirement`` — MEDIUM approximations until
      the content-marker fallback ports.

    Returns :attr:`Confidence.NONE` for unhandled intents and
    :attr:`Confidence.ABSTAIN` when a supported intent can't resolve
    its slots.
    """
    induced_markers = await _load_induced_markers(store)
    try:
        ctx = await vocabulary_cache.get_parser_context(  # type: ignore[attr-defined]
            store, induced_markers=induced_markers,
        )
    except Exception as exc:  # pragma: no cover — defensive guard
        logger.warning("TLG: could not build ParserContext: %s", exc)
        return LGTrace(
            query=query,
            intent=LGIntent(kind=""),
            confidence=Confidence.NONE,
            proof=f"parser context build failed: {exc!r}",
        )

    # Shape-cache fast path.  The cache stores skeleton → intent;
    # slot values still come from the actual query every time.
    qs = None
    cache_hit = False
    if shape_cache is not None:
        hit = shape_cache.lookup(query, ctx.vocabulary)  # type: ignore[attr-defined]
        if hit is not None:
            cached_intent, slots = hit
            qs = QueryStructure(
                intent=cached_intent,
                subject=ctx.vocabulary.subject_lookup.get(
                    slots.get("<X>", "").lower(),
                ) if slots else None,
                target_entity=slots.get("<X>"),
                secondary_entity=slots.get("<Y>"),
                detected_marker="shape_cache_hit",
            )
            cache_hit = True
    if qs is None:
        qs = analyze_query(query, ctx)
        if shape_cache is not None and qs.intent not in ("none", "abstain"):
            try:
                await shape_cache.learn(  # type: ignore[attr-defined]
                    store, query, qs.intent, ctx.vocabulary,
                )
            except Exception:  # pragma: no cover — defensive guard
                logger.debug("TLG: shape-cache learn failed", exc_info=True)

    intent = _intent_to_lg_intent(qs)
    trace = LGTrace(query=query, intent=intent)
    if cache_hit:
        trace.proof = f"shape-cache hit: intent={qs.intent}"

    if qs.intent == "none":
        trace.proof = "no LG intent matched; grammar did not apply"
        trace.confidence = Confidence.NONE
        return trace

    subject = qs.subject
    if subject is None:
        trace.proof = f"intent={qs.intent!r}: no subject inferred"
        trace.confidence = Confidence.ABSTAIN
        return trace

    # All dispatchers that do node-level work share this context.
    zones, node_index, zone_edges = await _load_subject_zones(store, subject)
    if not zones:
        trace.proof = f"{qs.intent}(subject={subject}): no ENTITY_STATE nodes"
        trace.confidence = Confidence.ABSTAIN
        return trace

    dispatch_ctx = _DispatchCtx(
        store=store,
        zones=zones,
        node_index=node_index,
        zone_edges=zone_edges,
        vocabulary_cache=vocabulary_cache,
    )
    try:
        await _route_intent(qs, trace, dispatch_ctx)
    except Exception as exc:  # pragma: no cover — defensive guard
        logger.warning(
            "TLG dispatch for intent=%s raised: %s", qs.intent, exc,
        )
        trace.proof = f"dispatcher raised: {exc!r}"
        trace.confidence = Confidence.ABSTAIN
    return trace


@dataclass
class _DispatchCtx:
    """Bundles everything every dispatcher needs.

    Built once per :func:`retrieve_lg` call so individual dispatchers
    stay small and uniformly-typed.
    """

    store: object
    zones: list
    node_index: dict[str, MemoryNode]
    zone_edges: list
    vocabulary_cache: object


async def _expand_entity_aliases(
    qs: QueryStructure, ctx: _DispatchCtx,
) -> frozenset[str] | None:
    """Safe alias lookup used by still/retirement dispatchers."""
    if qs.target_entity is None:
        return None
    try:
        return await ctx.vocabulary_cache.expand(  # type: ignore[attr-defined]
            qs.target_entity, ctx.store,
        )
    except Exception:  # pragma: no cover — defensive guard
        logger.debug("TLG: alias expansion failed", exc_info=True)
        return None


async def _dispatch_current_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    zone = current_zone(ctx.zones, ctx.node_index)
    if zone is None:
        trace.proof = f"current(subject={qs.subject}): no ungrounded zone"
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = zone.terminal_mid
    trace.zone_context = [
        mid for mid in zone.memory_ids if mid != zone.terminal_mid
    ]
    trace.admitted_zones = [f"zone{zone.zone_id}"]
    trace.proof = (
        f"current(subject={qs.subject}): terminal of zone "
        f"{zone.zone_id} (chain: {' -> '.join(zone.memory_ids)})"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_origin_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    root = origin_memory(ctx.zones, ctx.node_index)
    if root is None:
        trace.proof = f"origin(subject={qs.subject}): empty zones"
        trace.confidence = Confidence.ABSTAIN
        return
    earliest = next(
        (z for z in ctx.zones if z.start_mid == root), None,
    )
    if earliest is not None:
        trace.zone_context = [
            mid for mid in earliest.memory_ids if mid != root
        ]
        trace.admitted_zones = [f"zone{earliest.zone_id}"]
    trace.grammar_answer = root
    trace.proof = (
        f"origin(subject={qs.subject}): root of earliest zone = {root}"
    )
    trace.confidence = Confidence.HIGH


async def _still_retirement_match(
    qs: QueryStructure,
    ctx: _DispatchCtx,
    aliases: frozenset[str] | None,
) -> str | None:
    """Structural retirement lookup scoped to the subject's zones."""
    alias_map = (
        {qs.target_entity: aliases}
        if qs.target_entity is not None and aliases is not None
        else None
    )
    return retirement_memory(
        qs.target_entity or "",
        ctx.zone_edges,
        set(ctx.node_index.keys()),
        aliases=alias_map,
    )


async def _still_current_zone_hit(
    qs: QueryStructure,
    ctx: _DispatchCtx,
    aliases: frozenset[str] | None,
):
    """Medium-confidence fallback: entity linked to the current zone."""
    current = current_zone(ctx.zones, ctx.node_index)
    if current is None or qs.target_entity is None:
        return None
    needles = {qs.target_entity.lower()}
    if aliases:
        needles.update(a.lower() for a in aliases)
    for mid in current.memory_ids:
        node = ctx.node_index.get(mid)
        if node is None:
            continue
        linked = await ctx.store.get_memory_entities(node.memory_id)  # type: ignore[attr-defined]
        if _any_entity_covers_needle(linked, needles):
            return current
    return None


async def _dispatch_still_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    aliases = await _expand_entity_aliases(qs, ctx)
    retired_dst = await _still_retirement_match(qs, ctx, aliases)
    if retired_dst is not None:
        trace.grammar_answer = retired_dst
        trace.proof = (
            f"still(subject={qs.subject}, entity={qs.target_entity}): "
            f"retired by SUPERSEDES edge producing {retired_dst}"
        )
        trace.confidence = Confidence.HIGH
        return
    current = await _still_current_zone_hit(qs, ctx, aliases)
    if current is not None:
        trace.grammar_answer = current.terminal_mid
        trace.zone_context = [
            m for m in current.memory_ids if m != current.terminal_mid
        ]
        trace.admitted_zones = [f"zone{current.zone_id}"]
        trace.proof = (
            f"still(subject={qs.subject}, entity={qs.target_entity}): "
            f"entity in current zone {current.zone_id}"
        )
        trace.confidence = Confidence.MEDIUM
        return
    trace.proof = (
        f"still(subject={qs.subject}, entity={qs.target_entity}): "
        "no retirement and not in current zone"
    )
    trace.confidence = Confidence.ABSTAIN


async def _dispatch_retirement_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    aliases = await _expand_entity_aliases(qs, ctx)
    retired_dst = await _still_retirement_match(qs, ctx, aliases)
    if retired_dst is not None:
        trace.grammar_answer = retired_dst
        trace.proof = (
            f"retirement(subject={qs.subject}, entity="
            f"{qs.target_entity}): retired by edge to {retired_dst}"
        )
        trace.confidence = Confidence.HIGH
        return
    trace.proof = (
        f"retirement(subject={qs.subject}, entity="
        f"{qs.target_entity}): no matching retirement edge"
    )
    trace.confidence = Confidence.ABSTAIN


async def _dispatch_sequence_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    await _dispatch_sequence(
        ctx.store, trace,
        node_index=ctx.node_index, zone_edges=ctx.zone_edges,
    )


async def _dispatch_predecessor_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    await _dispatch_predecessor(
        ctx.store, trace,
        node_index=ctx.node_index, zone_edges=ctx.zone_edges,
    )


async def _dispatch_interval_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    await _dispatch_interval(ctx.store, trace, node_index=ctx.node_index)


async def _dispatch_range_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    await _dispatch_range(
        ctx.store, trace,
        node_index=ctx.node_index,
        range_start=qs.range_start,
        range_end=qs.range_end,
    )


async def _dispatch_before_named_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    await _dispatch_before_named(ctx.store, trace, node_index=ctx.node_index)


async def _dispatch_transitive_cause_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    await _dispatch_transitive_cause(
        ctx.store, trace,
        node_index=ctx.node_index, zone_edges=ctx.zone_edges,
    )


async def _dispatch_concurrent_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    await _dispatch_concurrent(ctx.store, trace, node_index=ctx.node_index)


async def _dispatch_cause_of_intent(
    trace: LGTrace, qs: QueryStructure, ctx: _DispatchCtx,
) -> None:
    await _dispatch_cause_of(ctx.store, trace, node_index=ctx.node_index)


_INTENT_DISPATCHERS = {
    "current": _dispatch_current_intent,
    "origin": _dispatch_origin_intent,
    "still": _dispatch_still_intent,
    "retirement": _dispatch_retirement_intent,
    "sequence": _dispatch_sequence_intent,
    "predecessor": _dispatch_predecessor_intent,
    "interval": _dispatch_interval_intent,
    "range": _dispatch_range_intent,
    "before_named": _dispatch_before_named_intent,
    "transitive_cause": _dispatch_transitive_cause_intent,
    "concurrent": _dispatch_concurrent_intent,
    "cause_of": _dispatch_cause_of_intent,
}


async def _route_intent(
    qs: QueryStructure,
    trace: LGTrace,
    ctx: _DispatchCtx,
) -> None:
    """Look up the dispatcher for ``qs.intent`` and invoke it.

    Unknown intents produce ``Confidence.NONE`` so the composition
    falls through to BM25 unchanged.
    """
    dispatcher = _INTENT_DISPATCHERS.get(qs.intent)
    if dispatcher is None:
        trace.proof = f"intent={qs.intent!r}: no dispatcher"
        trace.confidence = Confidence.NONE
        return
    await dispatcher(trace, qs, ctx)
