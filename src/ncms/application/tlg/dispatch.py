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
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subject-scoped helpers
# ---------------------------------------------------------------------------


async def _current_state_node(
    store: object, subject: str,
) -> MemoryNode | None:
    """Return the most recent is_current ENTITY_STATE node for ``subject``.

    When multiple current states exist (different ``state_key`` per
    entity), returns the newest by ``created_at`` — dispatch treats
    that as the subject's dominant current state.
    """
    nodes = await store.get_entity_states_by_entity(subject)  # type: ignore[attr-defined]
    current = [n for n in nodes if n.is_current]
    if not current:
        return None
    # get_entity_states_by_entity orders newest-first already.
    return current[0]


async def _origin_state_node(
    store: object, subject: str,
) -> MemoryNode | None:
    """Return the earliest ENTITY_STATE node for ``subject`` (zone root).

    Uses ``observed_at`` when set (real-world event time), falling
    back to ``created_at`` (NCMS ingest time).  Matches the research
    zone-root heuristic: the first memory for a subject.
    """
    nodes = await store.get_entity_states_by_entity(subject)  # type: ignore[attr-defined]
    if not nodes:
        return None

    def _sort_key(n: MemoryNode):
        return (n.observed_at or n.created_at)

    return min(nodes, key=_sort_key)


async def _retirement_edge_for_entity(
    store: object, subject: str, entity: str,
) -> str | None:
    """If ``entity`` has been retired within ``subject``, return the
    MemoryNode ID of the memory that retired it; else ``None``.

    Scans SUPERSEDES edges whose source is a state node belonging to
    ``subject`` and whose ``retires_entities`` contains ``entity``.
    The structural retirement set is populated by Phase 1 /
    Phase 3a — only SUPERSEDES edges born while TLG is enabled
    carry the set.
    """
    subject_nodes = await store.get_entity_states_by_entity(subject)  # type: ignore[attr-defined]
    entity_lower = entity.lower()
    for node in subject_nodes:
        edges = await store.get_graph_edges(node.id, EdgeType.SUPERSEDES)  # type: ignore[attr-defined]
        for edge in edges:
            for retired in edge.retires_entities:
                if retired.lower() == entity_lower:
                    return edge.source_id
    return None


# ---------------------------------------------------------------------------
# Intent dispatch
# ---------------------------------------------------------------------------


async def _dispatch_current(
    store: object, trace: LGTrace,
) -> None:
    if trace.intent.subject is None:
        trace.proof = "current: no subject inferred"
        trace.confidence = Confidence.ABSTAIN
        return
    node = await _current_state_node(store, trace.intent.subject)
    if node is None:
        trace.proof = (
            f"current(subject={trace.intent.subject}): "
            "no current ENTITY_STATE node found"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = node.id
    trace.proof = (
        f"current(subject={trace.intent.subject}): "
        f"is_current ENTITY_STATE node {node.id}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_origin(
    store: object, trace: LGTrace,
) -> None:
    if trace.intent.subject is None:
        trace.proof = "origin: no subject inferred"
        trace.confidence = Confidence.ABSTAIN
        return
    node = await _origin_state_node(store, trace.intent.subject)
    if node is None:
        trace.proof = (
            f"origin(subject={trace.intent.subject}): "
            "no ENTITY_STATE nodes for subject"
        )
        trace.confidence = Confidence.ABSTAIN
        return
    trace.grammar_answer = node.id
    trace.proof = (
        f"origin(subject={trace.intent.subject}): "
        f"earliest ENTITY_STATE node {node.id}"
    )
    trace.confidence = Confidence.HIGH


async def _dispatch_still(
    store: object, trace: LGTrace,
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

    # 1. Structural retirement — entity in a SUPERSEDES edge's
    #    retires_entities set.  Highest confidence: the state change
    #    was recorded with an explicit retirement annotation.
    retire_src = await _retirement_edge_for_entity(store, subject, entity)
    if retire_src is not None:
        trace.grammar_answer = retire_src
        trace.proof = (
            f"still(subject={subject}, entity={entity}): retired by "
            f"SUPERSEDES edge source {retire_src}"
        )
        trace.confidence = Confidence.HIGH
        return

    # 2. Entity-in-current-zone heuristic — if the entity is linked
    #    to the subject's current ENTITY_STATE memory, "still using"
    #    resolves to that current state.  Medium confidence: we didn't
    #    see a retirement, but we're relying on co-occurrence.
    current = await _current_state_node(store, subject)
    if current is not None:
        linked = await store.get_memory_entities(current.memory_id)  # type: ignore[attr-defined]
        if any(e.lower() == entity.lower() for e in linked):
            trace.grammar_answer = current.id
            trace.proof = (
                f"still(subject={subject}, entity={entity}): entity is "
                f"linked to the current state node {current.id}"
            )
            trace.confidence = Confidence.MEDIUM
            return

    trace.proof = (
        f"still(subject={subject}, entity={entity}): "
        "no retirement found and entity not linked to current state"
    )
    trace.confidence = Confidence.ABSTAIN


_INTENT_DISPATCHERS = {
    "current": _dispatch_current,
    "origin": _dispatch_origin,
    "still": _dispatch_still,
}


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

    try:
        await dispatcher(store, trace)
    except Exception as exc:  # pragma: no cover — defensive guard
        logger.warning(
            "TLG dispatch for intent=%s raised: %s", kind, exc,
        )
        trace.proof = f"dispatcher raised: {exc!r}"
        trace.confidence = Confidence.ABSTAIN
    return trace
