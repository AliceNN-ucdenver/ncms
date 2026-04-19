"""Query-side grammar dispatch (``retrieve_lg``).

Phase 3c implementation — handles three primitive intents
(``current`` / ``origin`` / ``still``) against the NCMS MemoryStore.
Uses the L1 vocabulary cache to resolve subject + entity, the L2
marker table via the retirement-extractor path (already wired in
Phase 3a) for supersession lookups, and the query_classifier for
intent detection.

Returns an :class:`LGTrace` that downstream composition
(:func:`ncms.domain.tlg.compose`) uses to decide whether to prepend
the grammar answer onto the BM25 ranking.  The core safety property
— zero confidently-wrong answers — is enforced by the confidence
level this module assigns: only ``HIGH`` / ``MEDIUM`` paths are
ever labelled safe for rank 1.

Intents not yet implemented (``sequence``, ``predecessor``,
``interval``, ``range``) land in later phases; dispatch returns
:attr:`Confidence.NONE` for them and the composition falls back to
BM25 unchanged.
"""

from __future__ import annotations

import logging

from ncms.domain.models import EdgeType, MemoryNode
from ncms.domain.tlg import (
    Confidence,
    LGIntent,
    LGTrace,
    classify_query_intent,
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


async def _dispatch_current(
    store: object, trace: LGTrace,
) -> None:
    subject = trace.intent.subject
    if subject is None:
        trace.proof = "current: no subject inferred"
        trace.confidence = Confidence.ABSTAIN
        return
    zones, node_index, _ = await _load_subject_zones(store, subject)
    if not zones:
        trace.proof = (
            f"current(subject={subject}): no ENTITY_STATE nodes"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    zone = current_zone(zones, node_index)
    if zone is None:
        trace.proof = (
            f"current(subject={subject}): every zone is closed "
            "(no ungrounded current)"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = zone.terminal_mid
    trace.zone_context = [
        mid for mid in zone.memory_ids if mid != zone.terminal_mid
    ]
    trace.admitted_zones = [f"zone{zone.zone_id}"]
    trace.proof = (
        f"current(subject={subject}): terminal of zone {zone.zone_id} "
        f"(chain: {' -> '.join(zone.memory_ids)})"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_origin(
    store: object, trace: LGTrace,
) -> None:
    subject = trace.intent.subject
    if subject is None:
        trace.proof = "origin: no subject inferred"
        trace.confidence = Confidence.ABSTAIN
        return
    zones, node_index, _ = await _load_subject_zones(store, subject)
    if not zones:
        trace.proof = (
            f"origin(subject={subject}): no ENTITY_STATE nodes"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    root = origin_memory(zones, node_index)
    if root is None:
        trace.proof = f"origin(subject={subject}): empty zone list"
        trace.confidence = Confidence.ABSTAIN
        return
    # zone_context: the rest of the earliest zone's chain.
    earliest = next((z for z in zones if z.start_mid == root), None)
    if earliest is not None:
        trace.zone_context = [
            mid for mid in earliest.memory_ids if mid != root
        ]
        trace.admitted_zones = [f"zone{earliest.zone_id}"]
    trace.grammar_answer = root
    trace.proof = (
        f"origin(subject={subject}): root of earliest zone = {root}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_still(
    store: object, trace: LGTrace,
    *,
    aliases: frozenset[str] | None = None,
) -> None:
    subject = trace.intent.subject
    entity = trace.intent.entity
    if subject is None:
        trace.proof = "still: no subject inferred"
        trace.confidence = Confidence.ABSTAIN
        return
    if entity is None:
        trace.proof = f"still(subject={subject}): no entity inferred"
        trace.confidence = Confidence.ABSTAIN
        return

    zones, node_index, zone_edges = await _load_subject_zones(
        store, subject,
    )
    if not zones:
        trace.proof = f"still(subject={subject}): no ENTITY_STATE nodes"
        trace.confidence = Confidence.ABSTAIN
        return

    # 1. Structural retirement — entity (or alias) in a SUPERSEDES
    #    edge's retires_entities set.  Uses the zones module's
    #    stem + alias + prefix matcher.
    alias_map: dict[str, frozenset[str]] | None = None
    if aliases:
        alias_map = {entity: aliases}
    retired_dst = retirement_memory(
        entity, zone_edges, set(node_index.keys()), aliases=alias_map,
    )
    if retired_dst is not None:
        trace.grammar_answer = retired_dst
        trace.proof = (
            f"still(subject={subject}, entity={entity}): retired by "
            f"SUPERSEDES edge producing {retired_dst}"
        )
        trace.confidence = Confidence.HIGH
        return

    # 2. Entity-in-current-zone heuristic — entity (or alias) is
    #    linked to any memory in the current zone.  Medium confidence:
    #    no explicit retirement, but co-occurrence is a strong hint.
    current = current_zone(zones, node_index)
    if current is not None:
        needles = {entity.lower()}
        if aliases:
            needles.update(a.lower() for a in aliases)
        for zone_member_id in current.memory_ids:
            node = node_index.get(zone_member_id)
            if node is None:
                continue
            linked = await store.get_memory_entities(node.memory_id)  # type: ignore[attr-defined]
            if any(e.lower() in needles for e in linked):
                trace.grammar_answer = current.terminal_mid
                trace.zone_context = [
                    mid for mid in current.memory_ids
                    if mid != current.terminal_mid
                ]
                trace.admitted_zones = [f"zone{current.zone_id}"]
                trace.proof = (
                    f"still(subject={subject}, entity={entity}): entity "
                    f"in current zone {current.zone_id}; terminal = "
                    f"{current.terminal_mid}"
                )
                trace.confidence = Confidence.MEDIUM
                return

    trace.proof = (
        f"still(subject={subject}, entity={entity}): "
        "no retirement found and entity not linked to current zone"
    )
    trace.confidence = Confidence.ABSTAIN


_INTENT_DISPATCHERS = {
    "current": _dispatch_current,
    "origin": _dispatch_origin,
    "still": _dispatch_still,
}

# Intents that benefit from alias expansion (entity-slot lookups).
_ALIAS_INTENTS: frozenset[str] = frozenset({"still"})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def retrieve_lg(
    query: str,
    *,
    store: object,
    vocabulary_cache: object,
) -> LGTrace:
    """Classify + dispatch a query against the grammar layer.

    Args:
      query: the user's natural-language query.
      store: a MemoryStore (async) — typically the shared SQLiteStore.
      vocabulary_cache: a
        :class:`~ncms.application.tlg.vocabulary_cache.VocabularyCache`
        or compatible object exposing async
        ``lookup_subject(query, store)`` and
        ``lookup_entity(query, store)``.

    Returns:
      An :class:`LGTrace` whose ``confidence`` indicates whether the
      caller may prepend ``grammar_answer`` onto the BM25 ranking.
      Never raises on missing data — unknown subjects, cold caches,
      and empty stores all produce :attr:`Confidence.NONE` or
      :attr:`Confidence.ABSTAIN` traces that the composition layer
      handles as "BM25 unchanged".
    """
    kind = classify_query_intent(query)
    subject = await vocabulary_cache.lookup_subject(query, store)  # type: ignore[attr-defined]
    entity = await vocabulary_cache.lookup_entity(query, store)  # type: ignore[attr-defined]
    intent = LGIntent(kind=kind or "", subject=subject, entity=entity)
    trace = LGTrace(query=query, intent=intent)

    if kind is None:
        trace.proof = "no LG intent matched; grammar did not apply"
        trace.confidence = Confidence.NONE
        return trace

    dispatcher = _INTENT_DISPATCHERS.get(kind)
    if dispatcher is None:
        trace.proof = f"intent={kind!r}: dispatcher not yet implemented"
        trace.confidence = Confidence.NONE
        return trace

    kwargs: dict = {}
    if kind in _ALIAS_INTENTS and entity is not None:
        try:
            kwargs["aliases"] = await vocabulary_cache.expand(entity, store)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover — defensive guard
            logger.debug(
                "TLG: alias expansion failed for entity=%s", entity,
                exc_info=True,
            )

    try:
        await dispatcher(store, trace, **kwargs)
    except Exception as exc:  # pragma: no cover — defensive guard
        logger.warning(
            "TLG dispatch for intent=%s raised: %s", kind, exc,
        )
        trace.proof = f"dispatcher raised: {exc!r}"
        trace.confidence = Confidence.ABSTAIN
    return trace
