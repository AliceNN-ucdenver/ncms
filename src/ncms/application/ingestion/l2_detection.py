"""L2 ENTITY_STATE node detection + creation.

Two strategies live here:

1. **Caller-asserted subject** (Option D' Part 4) — the caller knows
   the entity-subject of this memory; create L2 only when the SLM
   state_change head says ``declaration`` / ``retirement`` with
   confidence.
2. **No subject** — SLM-first state-change detection via
   :func:`slm_state_change_decision`, with regex fallback on
   cold-start deployments.

Extracted from :class:`IngestionPipeline` in the Phase F MI cleanup.
The pipeline keeps the public ``create_memory_nodes`` orchestrator;
these helpers are pure free functions.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from ncms.domain.models import EdgeType, GraphEdge, Memory, MemoryNode, NodeType

logger = logging.getLogger(__name__)


def _build_subject_l2_metadata(
    *,
    content: str,
    slm_label: dict,
    slm_state: str,
    subject: str,
) -> dict:
    """Build the L2 node metadata for the caller-subject path."""
    primary_span = None
    alt_span = None
    for r in slm_label.get("role_spans") or ():
        role = r.get("role")
        if role == "primary" and primary_span is None:
            primary_span = r
        elif role == "alternative" and alt_span is None:
            alt_span = r
    if primary_span:
        meta = {
            "entity_id": subject,
            "state_key": primary_span["slot"],
            "state_value": primary_span["canonical"],
            "source": "caller_subject_slm_role_span",
            "slm_state_change": slm_state,
        }
        if alt_span:
            meta["state_previous"] = alt_span["canonical"]
            meta["state_alternative"] = alt_span["canonical"]
        return meta
    snippet = content.strip()[:200] or "(empty)"
    return {
        "entity_id": subject,
        "state_key": "status",
        "state_value": snippet,
        "source": "caller_subject_slm_state_change",
        "slm_state_change": slm_state,
    }


def state_change_detected(
    *,
    config,
    slm_label: dict,
    content: str,
    admission_features: object | None,
) -> bool:
    """Decide whether the L2-creation path should run.

    SLM-first via the shared decision helper; falls through to regex
    only when the LoRA didn't run.
    """
    from ncms.domain.intent_slot_taxonomy import slm_state_change_decision

    slm_decision = slm_state_change_decision(slm_label, threshold=config.slm_confidence_threshold)
    if slm_decision is not None:
        has_state_change, has_state_declaration = slm_decision
        return has_state_change or has_state_declaration

    # Cold-start regex/heuristic fallback.
    has_state_change = (
        admission_features is not None
        and hasattr(admission_features, "state_change_signal")
        and admission_features.state_change_signal >= 0.35
    )
    has_state_declaration = bool(
        re.search(
            r"^[a-zA-Z0-9_\-]+\s*:\s*[a-zA-Z0-9_\-]+\s*=\s*.+$",
            content,
            re.MULTILINE,
        )
        or re.search(r"(?:^|\n)##?\s*[Ss]tatus\s*[\n:]\s*\w+", content)
        or re.search(r"^\s*status\s*:\s*\w+", content, re.MULTILINE | re.IGNORECASE)
    )
    return has_state_change or has_state_declaration


def _noop_emit(*_args, **_kwargs) -> None:
    """No-op stage emitter for callers that don't observe pipeline events."""


async def _save_l2_node(
    *,
    store,
    memory: Memory,
    l1_node: MemoryNode,
    node_metadata: dict,
    emit_stage: Callable | None,
    extra_event_fields: dict,
) -> MemoryNode:
    """Persist an L2 ENTITY_STATE node + DERIVED_FROM edge to L1."""
    l2_node = MemoryNode(
        memory_id=memory.id,
        node_type=NodeType.ENTITY_STATE,
        importance=memory.importance,
        metadata=node_metadata,
    )
    await store.save_memory_node(l2_node)
    await store.save_graph_edge(
        GraphEdge(
            source_id=l2_node.id,
            target_id=l1_node.id,
            edge_type=EdgeType.DERIVED_FROM,
            metadata={"layer": "L2_from_L1"},
        )
    )
    event_fields = {
        "node_id": l2_node.id,
        "node_type": "entity_state",
        "layer": "L2",
        "derived_from": l1_node.id,
    }
    event_fields.update(extra_event_fields)
    (emit_stage or _noop_emit)("memory_node", 0.0, event_fields, memory_id=memory.id)
    return l2_node


async def create_l2_with_subject(
    *,
    store,
    config,
    memory: Memory,
    content: str,
    l1_node: MemoryNode,
    slm_label: dict,
    subject: str,
    emit_stage: Callable | None = None,
) -> MemoryNode | None:
    """L2 path 1 — caller-asserted subject + SLM-confident state change."""
    slm_state_raw = slm_label.get("state_change")
    slm_state_conf = slm_label.get("state_change_confidence") or 0.0
    if not isinstance(slm_state_raw, str) or slm_state_raw not in {"declaration", "retirement"}:
        return None
    if slm_state_conf < config.slm_confidence_threshold:
        return None
    slm_state: str = slm_state_raw

    node_metadata = _build_subject_l2_metadata(
        content=content, slm_label=slm_label, slm_state=slm_state, subject=subject
    )
    return await _save_l2_node(
        store=store,
        memory=memory,
        l1_node=l1_node,
        node_metadata=node_metadata,
        emit_stage=emit_stage,
        extra_event_fields={
            "has_entity_state": True,
            "source": "caller_subject_slm_state_change",
        },
    )


async def create_l2_via_state_detection(
    *,
    store,
    config,
    extract_entity_state_meta_fn: Callable,
    memory: Memory,
    content: str,
    all_entities: list[dict],
    l1_node: MemoryNode,
    admission_features: object | None,
    slm_label: dict,
    emit_stage: Callable | None = None,
) -> MemoryNode | None:
    """L2 path 2 — SLM-first state detection (cold-start regex fallback)."""
    if not state_change_detected(
        config=config,
        slm_label=slm_label,
        content=content,
        admission_features=admission_features,
    ):
        return None

    node_metadata = extract_entity_state_meta_fn(content, all_entities, slm_label=slm_label)
    if not node_metadata:
        return None

    # Validate detected entity exists in GLiNER set, except when
    # metadata came from the SLM role span (canonical form may not
    # match GLiNER's mixed-case variant verbatim).
    if node_metadata.get("source") != "slm_role_span":
        entity_names_lower = {e["name"].lower() for e in all_entities}
        detected = node_metadata.get("entity_id", "")
        if detected.lower() not in entity_names_lower:
            return None

    return await _save_l2_node(
        store=store,
        memory=memory,
        l1_node=l1_node,
        node_metadata=node_metadata,
        emit_stage=emit_stage,
        extra_event_fields={
            "has_entity_state": bool(node_metadata.get("entity_id")),
        },
    )


# Section content types skip L2 creation — structural document content
# triggers false positives on state-declaration regexes.  Used by the
# async-indexing path; the inline ingestion path doesn't need this
# because its caller has already routed navigable content to the
# document store before reaching here.
SECTION_CONTENT_TYPES: frozenset[str] = frozenset(
    {"document_section", "document_chunk", "section_index", "document"}
)


async def detect_and_create_l2_node(
    *,
    store,
    config,
    extract_entity_state_meta_fn: Callable,
    memory: Memory,
    content: str,
    all_entities: list[dict],
    l1_node: MemoryNode,
    admission_features: object | None,
    emit_stage: Callable | None = None,
    subject: str | None = None,
    skip_section_memory_types: frozenset[str] | None = None,
) -> list[MemoryNode]:
    """Detect entity state change and create L2 ENTITY_STATE nodes.

    Phase A sub-PR 4 changed the return type to ``list[MemoryNode]``
    so multi-subject ingest can emit one L2 per affected timeline
    (claim A.6).  Single-subject ingest still returns a list of
    length 0 or 1; callers iterate uniformly.

    Dispatch order:

    1. **Multi-subject path (Phase A).** When
       ``memory.structured["subjects"]`` is populated, emit one L2
       per subject whose timeline has a state event (helper:
       :func:`multi_subject_l2.create_l2_nodes_for_subjects`) plus
       ``MENTIONS_ENTITY`` edges for *every* subject (helper:
       :func:`multi_subject_l2.create_mentions_entity_edges`).
    2. **Legacy caller-asserted subject** (single ``subject="..."``
       string, no Phase A bake — e.g. cold-start regressions).
    3. **Legacy SLM-state-detection** fallback.

    ``skip_section_memory_types`` short-circuits the legacy
    no-subject path when ``memory.type`` matches.  Used by the
    async-indexing path to avoid spurious L2 nodes on structural
    document content.  Pass :data:`SECTION_CONTENT_TYPES` to opt in.
    """
    slm_label = (memory.structured or {}).get("intent_slot") or {}

    # ── Path 1: Phase A multi-subject from structured payload ────────
    from ncms.application.ingestion.multi_subject_l2 import (
        create_l2_nodes_for_subjects,
        create_mentions_entity_edges,
        subjects_from_memory,
    )

    subjects = subjects_from_memory(memory)
    if subjects:
        l2_nodes = await create_l2_nodes_for_subjects(
            store=store,
            config=config,
            memory=memory,
            content=content,
            l1_node=l1_node,
            subjects=subjects,
            slm_label=slm_label,
            save_l2_fn=_save_l2_node,
            emit_stage=emit_stage,
        )
        await create_mentions_entity_edges(
            store=store,
            memory=memory,
            l1_node=l1_node,
            subjects=subjects,
            emit_stage=emit_stage,
        )
        return l2_nodes

    # ── Path 2: Legacy caller-asserted subject ───────────────────────
    if subject:
        l2 = await create_l2_with_subject(
            store=store,
            config=config,
            memory=memory,
            content=content,
            l1_node=l1_node,
            slm_label=slm_label,
            subject=subject,
            emit_stage=emit_stage,
        )
        return [l2] if l2 is not None else []

    # ── Path 3: Legacy SLM-detection fallback ────────────────────────
    if skip_section_memory_types and memory.type in skip_section_memory_types:
        return []
    l2 = await create_l2_via_state_detection(
        store=store,
        config=config,
        extract_entity_state_meta_fn=extract_entity_state_meta_fn,
        memory=memory,
        content=content,
        all_entities=all_entities,
        l1_node=l1_node,
        admission_features=admission_features,
        slm_label=slm_label,
        emit_stage=emit_stage,
    )
    return [l2] if l2 is not None else []
