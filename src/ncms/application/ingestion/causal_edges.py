"""CTLG causal-edge extraction (CAUSED_BY / ENABLES).

Reads ``intent_slot.cue_tags`` from the memory's structured payload,
runs the causal extractor, resolves cue surfaces to memory IDs via
the L2 ENTITY_STATE store, and persists typed graph edges.

Extracted from :class:`IngestionPipeline` in the Phase F MI cleanup.
No-op on pre-CTLG adapters (v9 ships ``cue: 0`` so ``cue_tags`` is
always empty); design at ``docs/research/ctlg-design.md``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ncms.domain.models import Memory, MemoryNode

logger = logging.getLogger(__name__)


def _parse_cue_tags(memory: Memory) -> list:
    """Deserialise ``intent_slot.cue_tags`` into TaggedToken dataclasses."""
    from ncms.domain.tlg.cue_taxonomy import TaggedToken

    slm_label = (memory.structured or {}).get("intent_slot") or {}
    cue_tag_dicts = slm_label.get("cue_tags") or []
    if not cue_tag_dicts:
        return []
    tokens: list[TaggedToken] = []
    for t in cue_tag_dicts:
        try:
            tokens.append(
                TaggedToken(
                    char_start=int(t["char_start"]),
                    char_end=int(t["char_end"]),
                    surface=str(t["surface"]),
                    cue_label=t["cue_label"],
                    confidence=float(t.get("confidence", 1.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tokens


def _extract_causal_pairs_safe(
    *,
    config,
    tokens: list,
    memory: Memory,
) -> list:
    """Wrap the causal-extractor in a try/except — best-effort."""
    from ncms.domain.tlg.causal_extractor import extract_causal_pairs

    try:
        return extract_causal_pairs(
            tokens,
            min_confidence=getattr(config, "ctlg_causal_min_confidence", 0.6),
        )
    except Exception:
        logger.warning("[ctlg] causal extraction failed for memory %s", memory.id, exc_info=True)
        return []


async def _build_causal_surface_lookup(
    *,
    store,
    memory: Memory,
    l2_node: MemoryNode | None,
    pairs: list,
) -> dict[str, str]:
    """Resolve cue surfaces → memory IDs (this memory + entity-state graph)."""
    lookup: dict[str, str] = {}
    if l2_node is not None:
        sv = str(l2_node.metadata.get("state_value", "")).lower().strip()
        eid = str(l2_node.metadata.get("entity_id", "")).lower().strip()
        if sv:
            lookup[sv] = memory.id
        if eid and eid not in lookup:
            lookup[eid] = memory.id

    needed: set[str] = set()
    for p in pairs:
        for surf in (p.effect_surface, p.cause_surface):
            surf_low = surf.lower()
            if surf_low not in lookup:
                needed.add(surf_low)

    if needed:
        try:
            l2_candidates = await store.get_memory_nodes_by_type("entity_state")
            for node in l2_candidates:
                sv = str(node.metadata.get("state_value", "")).lower().strip()
                eid = str(node.metadata.get("entity_id", "")).lower().strip()
                if sv and sv in needed and sv not in lookup:
                    lookup[sv] = node.memory_id
                if eid and eid in needed and eid not in lookup:
                    lookup[eid] = node.memory_id
        except Exception:
            logger.warning(
                "[ctlg] entity_state lookup failed — some pairs may be dropped",
                exc_info=True,
            )
    return lookup


async def _persist_causal_edges(
    *,
    store,
    pairs: list,
    lookup: dict[str, str],
    memory: Memory,
) -> tuple[int, int]:
    """Persist resolved causal pairs as typed GraphEdges."""
    from ncms.domain.models import EdgeType, GraphEdge
    from ncms.domain.tlg.causal_extractor import pairs_to_causal_edges

    edges = pairs_to_causal_edges(pairs, surface_to_memory_id=lookup)
    emitted = 0
    for edge in edges:
        edge_type = EdgeType.CAUSED_BY if edge.edge_type == "caused_by" else EdgeType.ENABLES
        try:
            await store.save_graph_edge(
                GraphEdge(
                    source_id=edge.src,
                    target_id=edge.dst,
                    edge_type=edge_type,
                    metadata={
                        "cue_type": edge.cue_type,
                        "source": "ctlg_cue_head",
                        "confidence": round(edge.confidence, 3),
                    },
                )
            )
            emitted += 1
        except Exception:
            logger.warning(
                "[ctlg] failed to persist causal edge %s -> %s",
                edge.src,
                edge.dst,
                exc_info=True,
            )
    return emitted, len(edges)


async def extract_and_persist_causal_edges(
    *,
    store,
    config,
    memory: Memory,
    l1_node: MemoryNode,
    l2_node: MemoryNode | None,
    emit_stage: Callable,
) -> None:
    """Extract CAUSED_BY / ENABLES edges from memory-voice cue tags.

    Gated on ``cue_tags`` presence in ``memory.structured["intent_slot"]``.
    No-op on pre-CTLG adapters.  Best-effort: any lookup error is
    logged and swallowed; a dropped pair doesn't break ingest.
    """
    tokens = _parse_cue_tags(memory)
    if not tokens:
        return

    t0 = time.perf_counter()
    pairs = _extract_causal_pairs_safe(config=config, tokens=tokens, memory=memory)
    if not pairs:
        return

    lookup = await _build_causal_surface_lookup(
        store=store, memory=memory, l2_node=l2_node, pairs=pairs
    )
    emitted, total_edges = await _persist_causal_edges(
        store=store, pairs=pairs, lookup=lookup, memory=memory
    )
    if emitted > 0:
        emit_stage(
            "ctlg_causal_edges",
            (time.perf_counter() - t0) * 1000,
            {
                "memory_id": memory.id,
                "n_pairs_extracted": len(pairs),
                "n_edges_persisted": emitted,
                "n_pairs_unresolved": len(pairs) - total_edges,
            },
            memory_id=memory.id,
        )
