"""L2 marker induction pipeline (application layer).

Composes the pure domain helpers in :mod:`ncms.domain.tlg.markers`
with the MemoryStore to run induction end-to-end:

1. Pull every SUPERSEDES / REFINES / SUPPORTS / CONFLICTS_WITH edge
   from the store.
2. Resolve each edge's ``source_id`` to the underlying Memory content
   — that's the "announcement" memory whose verbs we scan (the NEW
   state in an NCMS supersession, which corresponds to ``dst_content``
   in the research convention).
3. Build :class:`EdgeObservation` records and call
   :func:`induce_edge_markers`.
4. Persist the result into ``grammar_transition_markers`` (schema v12)
   via :meth:`SQLiteStore.save_transition_markers`.

Callers:

* **Batch job** (maintenance scheduler / CLI / startup) invokes
  :func:`induce_and_persist_markers` on demand or on a schedule.
  Induction is a full pass, not incremental — the distinctiveness
  filter requires global counts.
* **Reconciliation** reads the persisted table via
  :func:`load_retirement_verbs` on the hot path; falls back to
  :data:`SEED_RETIREMENT_VERBS` when the table is empty.

See ``docs/p1-plan.md`` §3 and
``docs/temporal-linguistic-geometry.md`` §5.
"""

from __future__ import annotations

import logging

from ncms.domain.models import EdgeType, GraphEdge
from ncms.domain.tlg import (
    SEED_RETIREMENT_VERBS,
    EdgeObservation,
    InducedEdgeMarkers,
    induce_edge_markers,
    retirement_verbs_from,
)

logger = logging.getLogger(__name__)


# Edge types that contribute observations to L2 induction.  We scan
# both directions of a supersession (SUPERSEDES from new→old AND
# SUPERSEDED_BY from old→new) — they carry the same announcement
# content because reconciliation sets both edges' source memory to
# the state-change announcement's memory in step with each other.
# Only SUPERSEDES is indexed here; SUPERSEDED_BY would double-count.
_INDUCTION_EDGE_TYPES: tuple[EdgeType, ...] = (
    EdgeType.SUPERSEDES,
    EdgeType.REFINES,
)


# NCMS edge-type → research transition-name.  Keep the mapping
# narrow: L2 induction cares about state-change transitions, not
# support/conflict/mention semantics.
_EDGE_TYPE_TO_TRANSITION: dict[str, str] = {
    EdgeType.SUPERSEDES.value: "supersedes",
    EdgeType.REFINES.value: "refines",
}


async def _observation_from_edge(
    store: object,
    edge: GraphEdge,
) -> EdgeObservation | None:
    """Build a single EdgeObservation, or None when data is missing.

    The source of an NCMS SUPERSEDES/REFINES edge is the
    announcement MemoryNode (the NEW state).  We resolve it to the
    backing Memory content — the announcement text is what carries
    the verb markers induction is mining for.
    """
    transition = _EDGE_TYPE_TO_TRANSITION.get(edge.edge_type.value)
    if transition is None:
        return None
    try:
        node = await store.get_memory_node(edge.source_id)  # type: ignore[attr-defined]
        if node is None:
            return None
        memory = await store.get_memory(node.memory_id)  # type: ignore[attr-defined]
        if memory is None or not memory.content:
            return None
    except Exception as exc:  # pragma: no cover — defensive guard
        logger.warning("TLG induction: could not resolve edge %s: %s", edge.id, exc)
        return None
    return EdgeObservation(transition=transition, dst_content=memory.content)


async def run_marker_induction(store: object) -> InducedEdgeMarkers:
    """Run L2 induction end-to-end and return the result.

    Pure read-path — does NOT write the result back to the store.
    Use :func:`induce_and_persist_markers` when you want the
    induction output written to ``grammar_transition_markers``.
    """
    edges = await store.list_graph_edges_by_type(  # type: ignore[attr-defined]
        [t.value for t in _INDUCTION_EDGE_TYPES]
    )
    observations: list[EdgeObservation] = []
    for edge in edges:
        obs = await _observation_from_edge(store, edge)
        if obs is not None:
            observations.append(obs)
    induced = induce_edge_markers(observations)
    logger.info(
        "TLG L2 induction produced markers for %d transitions (%d observations)",
        len(induced.markers),
        len(observations),
    )
    return induced


async def induce_and_persist_markers(store: object) -> InducedEdgeMarkers:
    """Run induction and write the result into
    ``grammar_transition_markers``.

    Batch-job entry point.  Safe to call repeatedly — the store
    method replaces the full table rather than merging, so marker
    drift is handled.
    """
    induced = await run_marker_induction(store)
    await store.save_transition_markers(induced.markers)  # type: ignore[attr-defined]
    return induced


async def load_retirement_verbs(store: object) -> frozenset[str]:
    """Return the verb set the retirement extractor should use.

    Reads ``grammar_transition_markers`` and flattens the
    ``supersedes`` + ``retires`` buckets.  Falls back to
    :data:`SEED_RETIREMENT_VERBS` when induction has not yet run
    (empty table) so reconciliation keeps working on cold deployments.
    """
    persisted = await store.load_transition_markers()  # type: ignore[attr-defined]
    if not persisted:
        return SEED_RETIREMENT_VERBS
    # Reuse the domain-layer helper that already knows which buckets
    # carry retirement signal.
    return retirement_verbs_from(InducedEdgeMarkers(markers=persisted))
