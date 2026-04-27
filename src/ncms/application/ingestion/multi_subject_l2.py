"""Multi-subject L2 emission + MENTIONS_ENTITY edges (Phase A sub-PR 4).

Claims A.6 + A.7.

When ``memory.structured["subjects"]`` is non-empty (post-Phase-A
ingest), this module's two helpers run in place of the legacy
single-subject L2 path:

* :func:`create_l2_nodes_for_subjects` — emits one L2
  ``ENTITY_STATE`` node per subject *whose timeline has a state
  event*.  A subject's timeline has a state event when (a) the
  SLM declared a state change AND (b) a role-span on the SLM
  output matches the subject's canonical surface or slot.  A
  subject without a matching role-span (the
  "auth-service is mentioned but not state-changing" case) gets
  no L2.

* :func:`create_mentions_entity_edges` — emits a
  ``MENTIONS_ENTITY`` graph edge from the L1 atomic node to each
  subject's :class:`Entity`, with ``metadata.role`` ∈
  ``{"primary_subject", "co_subject"}``.  Edges are emitted for
  *every* subject regardless of whether it has an L2.  Non-subject
  entity mentions (other GLiNER / SLM slot extractions) do NOT
  receive a role key — that's how downstream readers tell subject
  mentions apart from incidental ones.

Both helpers are pure functions that take an aiosqlite-bound
store; the file is imported by both
``application/ingestion/l2_detection.py`` (the inline path) and
``application/index_worker.py`` (the async path) so the inline /
async parity insurance stays intact.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ncms.domain.models import EdgeType, GraphEdge, Memory, MemoryNode, NodeType, Subject

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subject extraction from baked payload
# ---------------------------------------------------------------------------


def subjects_from_memory(memory: Memory) -> list[Subject]:
    """Reconstruct ``list[Subject]`` from ``memory.structured["subjects"]``.

    Returns an empty list when the payload is missing, empty, or
    contains malformed entries.  Malformed entries are skipped
    individually with a warning rather than failing the whole
    decode — this keeps multi-subject ingest robust to partial
    upgrades (e.g. a memory written by a buggy code path that
    skipped some fields).
    """
    raw = (memory.structured or {}).get("subjects") or []
    if not isinstance(raw, list):
        return []
    out: list[Subject] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            out.append(Subject(**d))
        except (TypeError, ValueError) as exc:
            logger.debug(
                "subjects_from_memory: skipping malformed payload entry %r (%s)",
                d,
                exc,
            )
            continue
    return out


# ---------------------------------------------------------------------------
# Role-span ↔ subject matching (private)
# ---------------------------------------------------------------------------


def _match_role_span_for_subject(
    s: Subject,
    role_spans: list[dict],
) -> dict | None:
    """Pick the SLM role-span that best identifies ``s``'s timeline.

    Match tiers (highest wins):

    1. **Canonical exact:** ``role_span.canonical`` (lowered) equals
       the subject's canonical-id slug or one of its aliases.
    2. **Surface exact:** same as tier 1 but compares the role-span's
       raw ``surface`` field.
    3. **Slot fallback:** ``role_span.slot`` equals
       ``subject.type``.  Used only when no exact span matches.

    Returns ``None`` when no role-span identifies this subject —
    meaning the subject is mentioned in passing (a co-subject)
    but does not have a state event on its timeline.
    """
    if not role_spans:
        return None
    aliases_lower = {a.lower() for a in s.aliases}
    slug = s.id.split(":", 1)[1].lower() if ":" in s.id else s.id.lower()

    # Tier 1: canonical match.
    for r in role_spans:
        canonical = (r.get("canonical") or "").lower()
        if canonical and (canonical == slug or canonical in aliases_lower):
            return r

    # Tier 2: surface match.
    for r in role_spans:
        surface = (r.get("surface") or "").lower()
        if surface and (surface == slug or surface in aliases_lower):
            return r

    # Tier 3: slot fallback.
    for r in role_spans:
        if r.get("slot") == s.type:
            return r

    return None


# ---------------------------------------------------------------------------
# L2 emission per subject (claim A.6)
# ---------------------------------------------------------------------------


def _build_subject_l2_metadata(
    *,
    s: Subject,
    matched_span: dict | None,
    content: str,
    state_change: str,
    is_only_caller_subject: bool,
) -> dict | None:
    """Build the L2 ``metadata`` dict for one subject, or None to skip.

    Skip rules:

    * No matched span AND not the only caller subject → return None
      (this is a co-subject mentioned in passing; no timeline event).
    * Otherwise build metadata with ``entity_id = s.id`` (canonical),
      role/state fields filled from the matched span when available,
      else falling back to a content snippet (legacy single-caller
      path).
    """
    if matched_span is None:
        if not (s.source == "caller" and is_only_caller_subject):
            return None
        snippet = content.strip()[:200] or "(empty)"
        return {
            "entity_id": s.id,
            "state_key": "status",
            "state_value": snippet,
            "source": "caller_subject_slm_state_change",
            "slm_state_change": state_change,
            "subject_role": "primary_subject" if s.primary else "co_subject",
        }
    return {
        "entity_id": s.id,
        "state_key": matched_span.get("slot") or "status",
        "state_value": (
            matched_span.get("canonical")
            or matched_span.get("surface")
            or ""
        ),
        "source": f"subject_{s.source}_role_span",
        "slm_state_change": state_change,
        "subject_role": "primary_subject" if s.primary else "co_subject",
    }


async def create_l2_nodes_for_subjects(
    *,
    store: Any,
    config: Any,
    memory: Memory,
    content: str,
    l1_node: MemoryNode,
    subjects: list[Subject],
    slm_label: dict,
    save_l2_fn: Callable[..., Any],
    emit_stage: Callable | None = None,
) -> list[MemoryNode]:
    """Emit one L2 ``ENTITY_STATE`` per subject whose timeline changed.

    Returns the list of L2 nodes created (may be empty when the
    SLM did not signal a state change, or when no subject has a
    matching role-span and there isn't a single-caller fallback).

    ``save_l2_fn`` is the shared persistence helper from
    ``l2_detection._save_l2_node`` — passed in to avoid a circular
    import.
    """
    state_change_raw = slm_label.get("state_change")
    if not isinstance(state_change_raw, str) or state_change_raw not in {
        "declaration",
        "retirement",
    }:
        return []
    state_conf = float(slm_label.get("state_change_confidence") or 0.0)
    threshold = float(getattr(config, "slm_confidence_threshold", 0.3) or 0.3)
    if state_conf < threshold:
        return []

    role_spans: list[dict] = list(slm_label.get("role_spans") or [])
    caller_subjects = [s for s in subjects if s.source == "caller"]
    is_solo_caller = len(caller_subjects) == 1 and len(subjects) == 1

    emitted: list[MemoryNode] = []
    for s in subjects:
        matched = _match_role_span_for_subject(s, role_spans)
        meta = _build_subject_l2_metadata(
            s=s,
            matched_span=matched,
            content=content,
            state_change=state_change_raw,
            is_only_caller_subject=is_solo_caller,
        )
        if meta is None:
            continue
        l2 = await save_l2_fn(
            store=store,
            memory=memory,
            l1_node=l1_node,
            node_metadata=meta,
            emit_stage=emit_stage,
            extra_event_fields={
                "has_entity_state": True,
                "source": meta["source"],
                "subject_id": s.id,
                "subject_role": meta["subject_role"],
            },
        )
        emitted.append(l2)
    return emitted


# ---------------------------------------------------------------------------
# MENTIONS_ENTITY edges with role metadata (claim A.7)
# ---------------------------------------------------------------------------


async def _find_subject_entity_id(
    store: Any,
    s: Subject,
) -> str | None:
    """Look up the persisted Entity row id for a subject.

    Match tiers (sub-PR 4 keeps both because the legacy
    ``subject=`` ingest path adds entities by raw alias surface;
    sub-PR 5 will tighten this once the inline / async entity-link
    blocks unify on canonical ids):

    1. Entity name == ``s.id`` (canonical id, post-Phase-A entry).
    2. Entity name == one of ``s.aliases`` (legacy raw-string
       entity-link path).
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

    For each subject in the list:

    * Resolve the subject's :class:`Entity` row id (by canonical id
      or alias — see :func:`_find_subject_entity_id`).
    * Insert a ``MENTIONS_ENTITY`` edge with ``metadata.role`` set
      to ``"primary_subject"`` (when ``s.primary``) or
      ``"co_subject"``.

    Subjects with no matching entity row are silently skipped —
    their canonicalization happened (``structured["subjects"]``
    has the entry) but the entity-link step didn't run for some
    reason (e.g. the inline indexing was bypassed).

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


# ---------------------------------------------------------------------------
# Re-export the L1 NodeType so callers don't need to import models too
# ---------------------------------------------------------------------------

__all__ = [
    "NodeType",
    "create_l2_nodes_for_subjects",
    "create_mentions_entity_edges",
    "subjects_from_memory",
]
