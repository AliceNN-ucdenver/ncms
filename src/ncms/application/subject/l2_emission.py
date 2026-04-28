"""Multi-subject L2 ENTITY_STATE node emission (claim A.6).

When ``memory.structured["subjects"]`` is non-empty, this module
emits one L2 ``ENTITY_STATE`` per subject *whose timeline has a
state event*.  A subject's timeline has a state event when:

1. The SLM declared a state change (``state_change`` ∈
   ``{"declaration", "retirement"}`` above the confidence
   threshold), AND
2. A ``role_span`` on the SLM output matches the subject — by
   canonical-id slug, by alias surface, or by slot type.

A subject without a matching role-span (the
"auth-service mentioned but not state-changing" case) gets no
L2.  The single-caller-subject fallback uses a content-snippet
``state_value`` when no role-span matches, preserving legacy
single-subject behaviour.

The helpers in this module are imported by both
``application/ingestion/l2_detection.py`` (the inline path) and
``application/index_worker.py`` (the async path) so the inline /
async parity insurance stays intact.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ncms.domain.models import Memory, MemoryNode, Subject

logger = logging.getLogger(__name__)


def subjects_from_memory(memory: Memory) -> list[Subject]:
    """Reconstruct ``list[Subject]`` from ``memory.structured["subjects"]``.

    Returns an empty list when the payload is missing, empty, or
    contains malformed entries.  Malformed entries are skipped
    individually with a debug log rather than failing the whole
    decode — keeps multi-subject ingest robust to partial upgrades.
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
                "subjects_from_memory: skipping malformed entry %r (%s)",
                d,
                exc,
            )
            continue
    return out


def _match_role_span_for_subject(
    s: Subject,
    role_spans: list[dict],
) -> dict | None:
    """Pick the SLM role-span that best identifies ``s``'s timeline.

    Match tiers (highest wins):

    1. **Canonical exact** — ``role_span.canonical`` (lowered)
       equals the subject's canonical-id slug or one of its
       aliases.
    2. **Surface exact** — same as tier 1 but compares the
       role-span's raw ``surface`` field.
    3. **Slot fallback** — ``role_span.slot`` equals
       ``subject.type``.  Used only when no exact span matches.

    Returns ``None`` when no role-span identifies this subject.
    """
    if not role_spans:
        return None
    aliases_lower = {a.lower() for a in s.aliases}
    slug = s.id.split(":", 1)[1].lower() if ":" in s.id else s.id.lower()

    for r in role_spans:
        canonical = (r.get("canonical") or "").lower()
        if canonical and (canonical == slug or canonical in aliases_lower):
            return r

    for r in role_spans:
        surface = (r.get("surface") or "").lower()
        if surface and (surface == slug or surface in aliases_lower):
            return r

    for r in role_spans:
        if r.get("slot") == s.type:
            return r

    return None


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

    * No matched span AND not the only caller subject → return
      None (mentioned in passing; no timeline event).
    * Otherwise build metadata with ``entity_id = s.id``
      (canonical), filling state fields from the matched span when
      present, falling back to a content snippet for the legacy
      single-caller path.
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
    """Emit one L2 per subject whose timeline has a state event.

    Returns the list of L2 nodes created (may be empty when the
    SLM did not signal a state change, or when no subject has a
    matching role-span and there isn't a single-caller fallback).

    ``save_l2_fn`` is the shared persistence helper (currently
    ``l2_detection._save_l2_node``); passed in as a parameter to
    avoid a circular import.
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
