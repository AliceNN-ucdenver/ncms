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

# CTLG v8+: causal edges the zone grammar consumes via G_tr,c.
# Loaded lazily by the causal dispatchers — not all queries need
# them so we avoid the full graph scan per call.
_CTLG_CAUSAL_EDGE_TYPES: frozenset[str] = frozenset(
    {
        EdgeType.CAUSED_BY.value,
        EdgeType.ENABLES.value,
    }
)


async def _load_causal_graph(store: object) -> list:
    """Load CAUSED_BY + ENABLES edges across the full store and
    return them as a list of :class:`CausalEdge`.

    CTLG causal zones cross subject boundaries (ctlg-grammar.md §7.3)
    so this loads the full causal subgraph, not just a subject-
    scoped slice.  Called once per :func:`retrieve_lg` invocation
    when the dispatcher enters a causal target; result is cached on
    the ``_DispatchCtx`` so later dispatchers reuse it.
    """
    from ncms.domain.tlg.zones import CausalEdge as _CausalEdge

    out: list = []
    # list_graph_edges_by_type takes a LIST of edge types — pass
    # all CTLG causal edge types in a single query.
    try:
        edges = await store.list_graph_edges_by_type(  # type: ignore[attr-defined]
            list(_CTLG_CAUSAL_EDGE_TYPES),
        )
    except Exception:
        logger.debug(
            "[tlg] list_graph_edges_by_type(CTLG) failed — falling back to empty causal graph",
            exc_info=True,
        )
        return out
    for e in edges:
        meta = getattr(e, "metadata", None) or {}
        out.append(
            _CausalEdge(
                src=e.source_id,
                dst=e.target_id,
                edge_type=e.edge_type.value if hasattr(e.edge_type, "value") else str(e.edge_type),
                cue_type=str(meta.get("cue_type", "")),
                confidence=float(meta.get("confidence", 1.0)),
            )
        )
    return out


async def _load_subject_zones(
    store: object,
    subject: str,
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
            zone_edges.append(
                _ZoneEdge(
                    src=zone_src,
                    dst=zone_dst,
                    transition=transition,
                    retires_entities=frozenset(edge.retires_entities),
                )
            )

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


# Re-exported from walkers for callers that historically imported
# these names from `dispatch` (test fixtures, benchmark glue).  The
# F401 suppressions keep ruff from stripping them as "unused".
from ncms.application.tlg.walkers import (  # noqa: E402
    _any_entity_covers_needle,  # noqa: F401
    _dispatch_before_named,
    _dispatch_cause_of,
    _dispatch_concurrent,
    _dispatch_interval,
    _dispatch_predecessor,
    _dispatch_range,
    _dispatch_sequence,
    _dispatch_transitive_cause,
    _find_event_node,  # noqa: F401
    _walk_causal_chain,  # noqa: F401
)

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


#: Map from :class:`ncms.domain.tlg.semantic_parser.TLGQuery.relation`
#: values to the ``qs.intent`` strings used by
#: ``_INTENT_DISPATCHERS`` below.  Most relations collapse onto a
#: shared walker: ``first`` + ``declared`` → ``origin``; ``last`` +
#: ``current`` → ``current``; every causal flavour → ``cause_of``.
#:
#: Unmapped relations (``would_be_current_if`` / ``could_have_been``
#: etc.) produce ``Confidence.NONE`` — the dispatcher has no
#: counterfactual walker yet (future work; see ctlg-design.md §5.4).
_TLG_RELATION_TO_DISPATCH_INTENT: dict[str, str] = {
    # state axis
    "current": "current",
    "retired": "retirement",
    "declared": "origin",
    # temporal axis
    "state_at": "current",
    "before_named": "before_named",
    "after_named": "sequence",
    "between": "interval",
    "concurrent_with": "concurrent",
    "during_interval": "interval",
    "predecessor": "predecessor",
    # causal axis
    "cause_of": "cause_of",
    "effect_of": "cause_of",
    "chain_cause_of": "transitive_cause",
    "trigger_of": "cause_of",
    "contributing_factor": "cause_of",
    # ordinal axis
    "first": "origin",
    "last": "current",
    "nth": "sequence",
    # modal axis (counterfactual) — no walker yet; grammar abstains.
}


async def _build_parser_context(
    *, query: str, store: object, vocabulary_cache: object
) -> tuple[object, LGTrace | None]:
    """Build the L3 parser context.  Returns (ctx, error_trace)."""
    induced_markers = await _load_induced_markers(store)
    try:
        ctx = await vocabulary_cache.get_parser_context(  # type: ignore[attr-defined]
            store,
            induced_markers=induced_markers,
        )
    except Exception as exc:  # pragma: no cover — defensive guard
        logger.warning("TLG: could not build ParserContext: %s", exc)
        return None, LGTrace(
            query=query,
            intent=LGIntent(kind=""),
            confidence=Confidence.NONE,
            proof=f"parser context build failed: {exc!r}",
        )
    return ctx, None


def _try_shape_cache_lookup(
    *, query: str, shape_cache: object | None, ctx: object
) -> tuple[QueryStructure | None, bool]:
    """Skeleton-match fast path.  Returns (qs, cache_hit)."""
    if shape_cache is None:
        return None, False
    hit = shape_cache.lookup(query, ctx.vocabulary)  # type: ignore[attr-defined]
    if hit is None:
        return None, False
    cached_intent, slots = hit
    qs = QueryStructure(
        intent=cached_intent,
        subject=(
            ctx.vocabulary.subject_lookup.get(slots.get("<X>", "").lower()) if slots else None
        ),
        target_entity=slots.get("<X>"),
        secondary_entity=slots.get("<Y>"),
        detected_marker="shape_cache_hit",
    )
    return qs, True


async def _apply_tlg_dispatch_overlay(
    *,
    query: str,
    qs: QueryStructure,
    tlg_query: object,
    shape_cache: object | None,
    store: object,
    ctx: object,
    trace: LGTrace,
) -> tuple[QueryStructure, LGTrace, LGTrace | None]:
    """Map a TLGQuery onto the dispatcher's walker intent.

    Returns ``(qs, trace, terminal_trace)`` — when ``terminal_trace`` is
    not None, retrieve_lg should return it directly (relation has no
    walker yet).
    """
    relation = getattr(tlg_query, "relation", None)
    mapped = _TLG_RELATION_TO_DISPATCH_INTENT.get(relation or "")
    if mapped is None:
        trace.proof = (
            f"TLG relation={relation!r}: no dispatcher (modal / "
            "counterfactual not yet walkable) — grammar abstains"
        )
        trace.confidence = Confidence.NONE
        return qs, trace, trace
    import dataclasses as _dc

    # Prefer the synthesizer's referent/subject when present —
    # analyze_query's heuristic extraction was only useful when
    # the dispatcher had no structured signal.
    new_subject = getattr(tlg_query, "subject", None) or qs.subject
    new_target = getattr(tlg_query, "referent", None) or qs.target_entity
    qs = _dc.replace(
        qs,
        intent=mapped,
        subject=new_subject,
        target_entity=new_target,
        detected_marker="tlg_synthesizer",
    )
    trace = LGTrace(query=query, intent=_intent_to_lg_intent(qs))
    trace.proof = (
        f"TLG axis={getattr(tlg_query, 'axis', '?')!r} "
        f"relation={relation!r} -> dispatch intent={mapped!r} "
        f"(rule={getattr(tlg_query, 'matched_rule', '')!r})"
    )
    # Shape-cache learn — cache the synthesizer-assigned intent
    # so the skeleton-match fast path picks it up on future queries.
    if shape_cache is not None:
        try:
            await shape_cache.learn(  # type: ignore[attr-defined]
                store,
                query,
                mapped,
                ctx.vocabulary,
            )
        except Exception:  # pragma: no cover — defensive guard
            logger.debug("TLG: shape-cache learn failed", exc_info=True)
    return qs, trace, None


def _grammar_abstain_trace(*, trace: LGTrace, slm_abstained: bool) -> LGTrace:
    if slm_abstained:
        trace.proof = "SLM cue head / synthesizer abstained; grammar does not apply"
    else:
        trace.proof = (
            "no TLGQuery supplied; grammar does not apply "
            "(regex + shape_intent classifiers deleted in v8.1)"
        )
    trace.confidence = Confidence.NONE
    return trace


async def retrieve_lg(
    query: str,
    *,
    store: object,
    vocabulary_cache: object,
    shape_cache: object | None = None,
    tlg_query: object | None = None,
    slm_abstained: bool = False,
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

    Returns :attr:`Confidence.NONE` for unhandled intents and
    :attr:`Confidence.ABSTAIN` when a supported intent can't resolve
    its slots.
    """
    ctx, err = await _build_parser_context(
        query=query, store=store, vocabulary_cache=vocabulary_cache
    )
    if err is not None:
        return err

    qs, cache_hit = _try_shape_cache_lookup(query=query, shape_cache=shape_cache, ctx=ctx)
    if qs is None:
        # analyze_query now produces subject + target_entity only
        # (post-v6).  Intent is always None on return; the SLM
        # override below fills it in.
        qs = analyze_query(query, ctx)

    trace = LGTrace(query=query, intent=LGIntent(kind=""))
    if cache_hit and qs.intent is not None:
        trace.proof = f"shape-cache hit: intent={qs.intent}"

    # ── CTLG dispatch (v8.1) — synthesizer-composed TLGQuery ──
    if tlg_query is not None:
        qs, trace, terminal = await _apply_tlg_dispatch_overlay(
            query=query,
            qs=qs,
            tlg_query=tlg_query,
            shape_cache=shape_cache,
            store=store,
            ctx=ctx,
            trace=trace,
        )
        if terminal is not None:
            return terminal
    else:
        return _grammar_abstain_trace(trace=trace, slm_abstained=slm_abstained)

    if qs.intent is None or qs.intent == "none":
        trace.proof = "no LG intent assigned; grammar does not apply"
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
            "TLG dispatch for intent=%s raised: %s",
            qs.intent,
            exc,
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
    # CTLG v8+ lazy causal-graph cache.  Loaded on first access by a
    # causal dispatcher (transitive_cause, cause_of, chain_cause_of)
    # via :meth:`get_causal_zones`.  ``None`` means "not yet loaded";
    # empty list means "loaded, no causal edges exist in this store"
    # (so fallback walker takes over).
    _causal_edges: list | None = None
    _causal_zones: list | None = None

    async def get_causal_edges(self) -> list:
        """Lazy-load the full causal-edge graph.  Cached per-call."""
        if self._causal_edges is None:
            self._causal_edges = await _load_causal_graph(self.store)
        return self._causal_edges

    async def get_causal_zones(self) -> list:
        """Lazy-build causal zones from the loaded edges.

        Returns a list of :class:`CausalZone` weakly-connected
        components; empty when no CAUSED_BY / ENABLES edges exist.
        """
        if self._causal_zones is not None:
            return self._causal_zones
        from ncms.domain.tlg.zones import build_causal_zones

        edges = await self.get_causal_edges()
        self._causal_zones = build_causal_zones(edges) if edges else []
        return self._causal_zones


async def _expand_entity_aliases(
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> frozenset[str] | None:
    """Safe alias lookup used by still/retirement dispatchers."""
    if qs.target_entity is None:
        return None
    try:
        return await ctx.vocabulary_cache.expand(  # type: ignore[attr-defined]
            qs.target_entity,
            ctx.store,
        )
    except Exception:  # pragma: no cover — defensive guard
        logger.debug("TLG: alias expansion failed", exc_info=True)
        return None


async def _dispatch_current_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    zone = current_zone(ctx.zones, ctx.node_index)
    if zone is None:
        trace.proof = f"current(subject={qs.subject}): no ungrounded zone"
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = zone.terminal_mid
    trace.zone_context = [mid for mid in zone.memory_ids if mid != zone.terminal_mid]
    trace.admitted_zones = [f"zone{zone.zone_id}"]
    trace.proof = (
        f"current(subject={qs.subject}): terminal of zone "
        f"{zone.zone_id} (chain: {' -> '.join(zone.memory_ids)})"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_origin_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    root = origin_memory(ctx.zones, ctx.node_index)
    if root is None:
        trace.proof = f"origin(subject={qs.subject}): empty zones"
        trace.confidence = Confidence.ABSTAIN
        return
    earliest = next(
        (z for z in ctx.zones if z.start_mid == root),
        None,
    )
    if earliest is not None:
        trace.zone_context = [mid for mid in earliest.memory_ids if mid != root]
        trace.admitted_zones = [f"zone{earliest.zone_id}"]
    trace.grammar_answer = root
    trace.proof = f"origin(subject={qs.subject}): root of earliest zone = {root}"
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
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
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
        trace.zone_context = [m for m in current.memory_ids if m != current.terminal_mid]
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
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
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
        f"retirement(subject={qs.subject}, entity={qs.target_entity}): no matching retirement edge"
    )
    trace.confidence = Confidence.ABSTAIN


async def _dispatch_sequence_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    await _dispatch_sequence(
        ctx.store,
        trace,
        node_index=ctx.node_index,
        zone_edges=ctx.zone_edges,
    )


async def _dispatch_predecessor_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    await _dispatch_predecessor(
        ctx.store,
        trace,
        node_index=ctx.node_index,
        zone_edges=ctx.zone_edges,
    )


async def _dispatch_interval_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    await _dispatch_interval(ctx.store, trace, node_index=ctx.node_index)


async def _dispatch_range_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    await _dispatch_range(
        ctx.store,
        trace,
        node_index=ctx.node_index,
        range_start=qs.range_start,
        range_end=qs.range_end,
    )


async def _dispatch_before_named_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    await _dispatch_before_named(ctx.store, trace, node_index=ctx.node_index)


async def _dispatch_transitive_cause_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    await _dispatch_transitive_cause(
        ctx.store,
        trace,
        node_index=ctx.node_index,
        zone_edges=ctx.zone_edges,
        ctx=ctx,
    )


async def _dispatch_concurrent_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    await _dispatch_concurrent(ctx.store, trace, node_index=ctx.node_index)


async def _dispatch_cause_of_intent(
    trace: LGTrace,
    qs: QueryStructure,
    ctx: _DispatchCtx,
) -> None:
    await _dispatch_cause_of(ctx.store, trace, node_index=ctx.node_index)


_INTENT_DISPATCHERS = {
    "current": _dispatch_current_intent,
    "origin": _dispatch_origin_intent,
    "retirement": _dispatch_retirement_intent,
    "sequence": _dispatch_sequence_intent,
    "predecessor": _dispatch_predecessor_intent,
    "interval": _dispatch_interval_intent,
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
