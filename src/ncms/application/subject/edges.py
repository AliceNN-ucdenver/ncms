"""``MENTIONS_ENTITY`` graph edges with role metadata (claim A.7).

For each subject in ``memory.structured["subjects"]``, emit a
``MENTIONS_ENTITY`` edge from the L1 atomic node to the subject's
:class:`Entity`, with ``metadata.role`` ∈
``{"primary_subject", "co_subject"}``.  Edges are emitted for
*every* subject regardless of whether it has an L2 — co-subjects
mentioned in passing still need to be discoverable in the graph.

Non-subject entity mentions (other GLiNER / SLM slot extractions
that aren't subjects) do NOT receive a ``MENTIONS_ENTITY`` edge —
that's how downstream readers tell subject mentions apart from
incidental ones.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ncms.domain.models import EdgeType, GraphEdge, Memory, MemoryNode, Subject

logger = logging.getLogger(__name__)


async def _find_subject_entity_id(store: Any, s: Subject) -> str | None:
    """Look up the persisted Entity row id for a subject.

    Match tiers:

    1. Entity name == ``s.id`` (canonical, post-Phase-A entry).
    2. Entity name == one of ``s.aliases`` (legacy raw-string
       entity-link path).

    Returns ``None`` when no matching entity is found — caller
    skips edge creation rather than fabricating a target.
    """
    e = await store.find_entity_by_name(s.id)
    if e is not None:
        return e.id
    for alias in s.aliases:
        e = await store.find_entity_by_name(alias)
        if e is not None:
            return e.id
    return None


async def create_mentions_entity_edges(
    *,
    store: Any,
    memory: Memory,
    l1_node: MemoryNode,
    subjects: list[Subject],
    emit_stage: Callable | None = None,
) -> int:
    """Emit ``MENTIONS_ENTITY`` edges from L1 to each subject's Entity.

    Subjects with no matching entity row are silently skipped —
    their canonicalization happened (the structured payload has
    the entry) but the entity-link step didn't run for some
    reason.

    Returns the count of edges actually written.
    """
    if not subjects:
        return 0
    written = 0
    for s in subjects:
        entity_id = await _find_subject_entity_id(store, s)
        if entity_id is None:
            continue
        edge = GraphEdge(
            source_id=l1_node.id,
            target_id=entity_id,
            edge_type=EdgeType.MENTIONS_ENTITY,
            metadata={
                "role": "primary_subject" if s.primary else "co_subject",
                "subject_id": s.id,
                "subject_type": s.type,
                "source": s.source,
            },
        )
        await store.save_graph_edge(edge)
        written += 1
    if emit_stage is not None and written:
        emit_stage(
            "mentions_entity",
            0.0,
            {
                "edges_written": written,
                "subject_ids": [s.id for s in subjects],
            },
            memory_id=memory.id,
        )
    return written
