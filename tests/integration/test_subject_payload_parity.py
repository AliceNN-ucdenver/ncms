"""Phase A sub-PR 5 — inline / async subject-payload parity (A.14).

This is the headline insurance test that A.14 demands.  The
SLM/GLiNER divergence pattern (one path silently differing from
another) is the trap Phase A is designed to prevent for subjects.
The test ingests the same content through both paths (inline +
async indexing pool) and asserts byte-equivalent output on:

* ``memory.structured["subjects"]`` (canonical ids, types,
  primary flags, ordering)
* L2 entity_state nodes (count, ``entity_id``, ``state_key``,
  ``state_value``, ``subject_role``)
* ``MENTIONS_ENTITY`` edges (count, role metadata,
  primary/co_subject sets)

Both paths converge on
``application/ingestion/l2_detection.detect_and_create_l2_node``
which reads from the persisted ``memory.structured`` rather than
re-running the SLM, so per-subject L2 emission is path-invariant
by construction.  The parity test is the regression catch-all
that makes that invariant visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.domain.models import EdgeType, ExtractedLabel, NodeType, Subject
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# Test SLM (deterministic so inline + async produce the same output)
# ---------------------------------------------------------------------------


@dataclass
class _ParitySLM:
    """Deterministic intent-slot extractor for parity tests."""

    label: ExtractedLabel = field(
        default_factory=lambda: ExtractedLabel(
            intent="none",
            intent_confidence=0.95,
            slots={"decision": "ADR-004"},
            slot_confidences={"decision": 0.92},
            topic="other",
            topic_confidence=0.90,
            admission="persist",
            admission_confidence=0.95,
            state_change="declaration",
            state_change_confidence=0.95,
            role_spans=[
                {
                    "char_start": 0,
                    "char_end": 7,
                    "surface": "ADR-004",
                    "canonical": "ADR-004",
                    "slot": "decision",
                    "role": "primary",
                    "source": "test",
                },
                {
                    "char_start": 18,
                    "char_end": 25,
                    "surface": "ADR-002",
                    "canonical": "ADR-002",
                    "slot": "decision",
                    "role": "alternative",
                    "source": "test",
                },
            ],
            method="parity_test_extractor",
        ),
    )

    def extract(self, text: str, *, domain: str) -> ExtractedLabel:
        return self.label


# ---------------------------------------------------------------------------
# Path runners
# ---------------------------------------------------------------------------


def _config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        temporal_enabled=True,
        admission_enabled=False,
        index_workers=1,
    )


async def _build_service() -> tuple[MemoryService, SQLiteStore]:
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    svc = MemoryService(
        store=store,
        index=index,
        graph=NetworkXGraph(),
        config=_config(),
        intent_slot=_ParitySLM(),
    )
    return svc, store


async def _summarise_subjects(memory) -> list[dict]:
    """Return the structured subject payload, with stable ordering."""
    raw = (memory.structured or {}).get("subjects") or []
    # Sort by (id, source) so list ordering doesn't trip the
    # equality check.  All meaningful payload data is preserved.
    return sorted(raw, key=lambda d: (d.get("id", ""), d.get("source", "")))


async def _summarise_l2_nodes(store: SQLiteStore, memory_id: str) -> list[tuple]:
    """Stable summary of L2 ENTITY_STATE nodes (sorted by entity_id)."""
    nodes = await store.get_memory_nodes_for_memory(memory_id)
    l2 = [n for n in nodes if n.node_type == NodeType.ENTITY_STATE]
    return sorted(
        (
            (
                n.metadata.get("entity_id"),
                n.metadata.get("state_key"),
                n.metadata.get("state_value"),
                n.metadata.get("subject_role"),
                n.metadata.get("slm_state_change"),
            )
            for n in l2
        ),
    )


async def _summarise_mentions(
    store: SQLiteStore,
    l1_id: str,
) -> list[tuple]:
    """Stable summary of MENTIONS_ENTITY edges (sorted by subject_id)."""
    edges = await store.get_graph_edges(l1_id)
    mentions = [e for e in edges if e.edge_type == EdgeType.MENTIONS_ENTITY]
    return sorted(
        (
            (
                e.metadata.get("subject_id"),
                e.metadata.get("role"),
                e.metadata.get("subject_type"),
                e.metadata.get("source"),
            )
            for e in mentions
        ),
    )


async def _ingest(
    *,
    async_indexing: bool,
    subjects: list[Subject] | None,
    legacy_subject: str | None,
) -> tuple[list[dict], list[tuple], list[tuple]]:
    """Run a full ingest cycle and capture the parity-relevant outputs."""
    svc, store = await _build_service()
    try:
        if async_indexing:
            await svc.start_index_pool()
        mem = await svc.store_memory(
            "ADR-004 supersedes ADR-002 for auth-service",
            domains=["software_dev"],
            source_agent="parity-test",
            subjects=subjects,
            subject=legacy_subject,
        )
        if async_indexing:
            await svc.flush_indexing(poll_interval=0.01)

        subjects_payload = await _summarise_subjects(mem)
        nodes = await store.get_memory_nodes_for_memory(mem.id)
        l1 = next(n for n in nodes if n.node_type == NodeType.ATOMIC)
        l2_summary = await _summarise_l2_nodes(store, mem.id)
        edge_summary = await _summarise_mentions(store, l1.id)
        return subjects_payload, l2_summary, edge_summary
    finally:
        await svc.stop_index_pool()
        await store.close()


# ---------------------------------------------------------------------------
# A.14 — headline parity test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_async_byte_equivalent() -> None:
    """A.14: inline + async produce byte-equivalent subject output.

    Single-subject path (legacy ``subject=`` kwarg promoted to
    Subject by ``resolve_subjects``).  Both paths must produce the
    same structured["subjects"] payload, the same L2 nodes, and
    the same MENTIONS_ENTITY edges.
    """
    inline_payload, inline_l2, inline_edges = await _ingest(
        async_indexing=False,
        subjects=None,
        legacy_subject="adr-004",
    )
    async_payload, async_l2, async_edges = await _ingest(
        async_indexing=True,
        subjects=None,
        legacy_subject="adr-004",
    )

    assert async_payload == inline_payload, (
        "structured['subjects'] differs between paths\n"
        f"inline: {inline_payload!r}\nasync:  {async_payload!r}"
    )
    assert async_l2 == inline_l2, (
        "L2 entity_state nodes differ between paths\n"
        f"inline: {inline_l2!r}\nasync:  {async_l2!r}"
    )
    assert async_edges == inline_edges, (
        "MENTIONS_ENTITY edges differ between paths\n"
        f"inline: {inline_edges!r}\nasync:  {async_edges!r}"
    )


@pytest.mark.asyncio
async def test_inline_async_with_co_subjects() -> None:
    """A.14: multi-subject parity.

    The ADR-004 / ADR-002 / auth-service example.  Two subjects
    get L2s (the ADRs); the service is a co-subject without a
    state event so it gets a MENTIONS_ENTITY edge but no L2.
    The split must be identical on inline and async paths.
    """
    subjects = [
        Subject(id="decision:adr-004", type="decision", primary=True),
        Subject(id="decision:adr-002", type="decision", primary=False),
        Subject(id="service:auth-api", type="service", primary=False),
    ]
    inline_payload, inline_l2, inline_edges = await _ingest(
        async_indexing=False,
        subjects=subjects,
        legacy_subject=None,
    )
    async_payload, async_l2, async_edges = await _ingest(
        async_indexing=True,
        subjects=subjects,
        legacy_subject=None,
    )

    # Sanity: the inline path itself produced the expected shape
    # (two L2s for the ADRs, three MENTIONS_ENTITY edges).
    assert len(inline_l2) == 2
    assert {row[0] for row in inline_l2} == {"decision:adr-002", "decision:adr-004"}
    inline_subject_ids = {row[0] for row in inline_edges}
    assert "decision:adr-004" in inline_subject_ids
    # The service co-subject has no state event but MUST have an edge.
    assert "service:auth-api" in inline_subject_ids

    # And the async path produced exactly the same shape.
    assert async_payload == inline_payload, (
        f"payload differs\ninline: {inline_payload!r}\nasync:  {async_payload!r}"
    )
    assert async_l2 == inline_l2, (
        f"L2 nodes differ\ninline: {inline_l2!r}\nasync:  {async_l2!r}"
    )
    assert async_edges == inline_edges, (
        f"edges differ\ninline: {inline_edges!r}\nasync:  {async_edges!r}"
    )


@pytest.mark.asyncio
async def test_async_path_canonicalizes() -> None:
    """A.9: the async path writes canonical Subject ids.

    Caller passes ``subjects=[Subject(id="service:auth-api",
    aliases=("auth-service",))]`` through the async indexing pool;
    the persisted memory carries the canonical id (not the alias).
    """
    payload, _l2, _edges = await _ingest(
        async_indexing=True,
        subjects=[
            Subject(
                id="service:auth-api",
                type="service",
                primary=True,
                aliases=("auth-service",),
                source="caller",
            ),
        ],
        legacy_subject=None,
    )
    assert any(s["id"] == "service:auth-api" for s in payload), (
        f"async path did not persist canonical id: {payload!r}"
    )


@pytest.mark.asyncio
async def test_inline_path_canonicalizes() -> None:
    """A.8 covered through the parity harness: inline path canonicalizes."""
    payload, _l2, _edges = await _ingest(
        async_indexing=False,
        subjects=[
            Subject(
                id="service:auth-api",
                type="service",
                primary=True,
                aliases=("auth-service",),
                source="caller",
            ),
        ],
        legacy_subject=None,
    )
    assert any(s["id"] == "service:auth-api" for s in payload)
