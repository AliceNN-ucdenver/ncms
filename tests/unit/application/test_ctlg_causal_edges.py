"""Unit tests for ingest-side CTLG causal edge persistence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ncms.application.ingestion.causal_edges import extract_and_persist_causal_edges
from ncms.domain.models import EdgeType, GraphEdge, Memory, MemoryNode, NodeType


def _cue(
    surface: str,
    label: str,
    start: int,
    *,
    confidence: float = 0.9,
) -> dict:
    return {
        "char_start": start,
        "char_end": start + len(surface),
        "surface": surface,
        "cue_label": label,
        "confidence": confidence,
    }


class _FakeStore:
    def __init__(self, nodes: list[MemoryNode]) -> None:
        self.nodes = nodes
        self.edges: list[GraphEdge] = []

    async def get_memory_nodes_by_type(self, node_type: str) -> list[MemoryNode]:
        assert node_type == NodeType.ENTITY_STATE
        return self.nodes

    async def save_graph_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)


@pytest.mark.asyncio
async def test_extract_and_persist_causal_edges_writes_resolved_edge_with_provenance() -> None:
    memory = Memory(
        id="m_rewrite",
        content="outage led to rewrite",
        structured={
            "ctlg": {
                "cue_tags": [
                    _cue("outage", "B-REFERENT", 0),
                    _cue("led", "B-CAUSAL_ALTLEX", 7),
                    _cue("to", "I-CAUSAL_ALTLEX", 11),
                    _cue("rewrite", "B-REFERENT", 14),
                ]
            }
        },
    )
    l2_node = MemoryNode(
        id="n_rewrite",
        memory_id="m_rewrite",
        node_type=NodeType.ENTITY_STATE,
        metadata={"state_value": "rewrite", "entity_id": "auth-service"},
    )
    store = _FakeStore(
        [
            MemoryNode(
                id="n_outage",
                memory_id="m_outage",
                node_type=NodeType.ENTITY_STATE,
                metadata={"state_value": "outage", "entity_id": "incident"},
            )
        ]
    )
    stages: list[tuple[str, dict]] = []

    await extract_and_persist_causal_edges(
        store=store,
        config=SimpleNamespace(ctlg_causal_min_confidence=0.6),
        memory=memory,
        l1_node=MemoryNode(memory_id="m_rewrite", node_type=NodeType.ATOMIC),
        l2_node=l2_node,
        emit_stage=lambda name, _ms, payload, **_kw: stages.append((name, payload)),
    )

    assert len(store.edges) == 1
    edge = store.edges[0]
    assert edge.source_id == "m_rewrite"
    assert edge.target_id == "m_outage"
    assert edge.edge_type == EdgeType.CAUSED_BY
    assert edge.metadata == {
        "schema_version": 1,
        "cue_type": "CAUSAL_ALTLEX",
        "cue_surface": "led to",
        "cue_char_start": 7,
        "cue_char_end": 13,
        "source": "ctlg_cue_head",
        "confidence": 0.9,
        "ingest_memory_id": "m_rewrite",
    }
    assert stages[0][0] == "ctlg_causal_edges"
    assert stages[0][1]["n_edges_persisted"] == 1


@pytest.mark.asyncio
async def test_extract_and_persist_causal_edges_drops_unresolved_pairs() -> None:
    memory = Memory(
        id="m_rewrite",
        content="outage led to rewrite",
        structured={
            "ctlg": {
                "cue_tags": [
                    _cue("outage", "B-REFERENT", 0),
                    _cue("led", "B-CAUSAL_ALTLEX", 7),
                    _cue("to", "I-CAUSAL_ALTLEX", 11),
                    _cue("rewrite", "B-REFERENT", 14),
                ]
            }
        },
    )
    l2_node = MemoryNode(
        id="n_rewrite",
        memory_id="m_rewrite",
        node_type=NodeType.ENTITY_STATE,
        metadata={"state_value": "rewrite"},
    )
    store = _FakeStore([])
    stages: list[tuple[str, dict]] = []

    await extract_and_persist_causal_edges(
        store=store,
        config=SimpleNamespace(ctlg_causal_min_confidence=0.6),
        memory=memory,
        l1_node=MemoryNode(memory_id="m_rewrite", node_type=NodeType.ATOMIC),
        l2_node=l2_node,
        emit_stage=lambda name, _ms, payload, **_kw: stages.append((name, payload)),
    )

    assert store.edges == []
    assert stages == []
