"""Unit tests for the CTLG causal walker in application/tlg/dispatch.

Exercises ``_walk_causal_chain`` directly + the CTLG-path vs
pre-CTLG fallback logic in ``_dispatch_transitive_cause``.  Uses
minimal fakes instead of real stores so tests stay fast (no
sqlite, no network).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ncms.application.tlg.dispatch import (
    Confidence,
    LGIntent,
    LGTrace,
    _dispatch_transitive_cause,
    _walk_causal_chain,
)
from ncms.domain.tlg.zones import CausalEdge


class TestWalkCausalChain:
    @pytest.mark.asyncio
    async def test_single_edge_chain(self) -> None:
        edges = [CausalEdge(src="effect", dst="cause", edge_type="caused_by")]
        path, edge_types = await _walk_causal_chain("effect", edges)
        assert path == ["effect", "cause"]
        assert edge_types == ["caused_by"]

    @pytest.mark.asyncio
    async def test_three_step_chain(self) -> None:
        edges = [
            CausalEdge(src="m_rewrite", dst="m_outage", edge_type="caused_by"),
            CausalEdge(src="m_outage", dst="m_audit", edge_type="caused_by"),
        ]
        path, edge_types = await _walk_causal_chain("m_rewrite", edges)
        assert path == ["m_rewrite", "m_outage", "m_audit"]
        assert edge_types == ["caused_by", "caused_by"]

    @pytest.mark.asyncio
    async def test_no_outgoing_edge_stops(self) -> None:
        edges = [CausalEdge(src="other", dst="other_cause", edge_type="caused_by")]
        path, edge_types = await _walk_causal_chain("alone", edges)
        assert path == ["alone"]
        assert edge_types == []

    @pytest.mark.asyncio
    async def test_cycle_detection(self) -> None:
        # X -caused_by-> Y -caused_by-> X (cycle — shouldn't happen but guard).
        edges = [
            CausalEdge(src="x", dst="y", edge_type="caused_by"),
            CausalEdge(src="y", dst="x", edge_type="caused_by"),
        ]
        path, _ = await _walk_causal_chain("x", edges)
        # Walk stops when it would revisit X.
        assert path == ["x", "y"]

    @pytest.mark.asyncio
    async def test_max_depth_cap(self) -> None:
        # Build a 10-step chain and cap at depth 3.
        edges = [CausalEdge(src=f"m{i}", dst=f"m{i + 1}", edge_type="caused_by") for i in range(10)]
        path, _ = await _walk_causal_chain("m0", edges, max_depth=3)
        assert path == ["m0", "m1", "m2", "m3"]

    @pytest.mark.asyncio
    async def test_picks_highest_confidence_edge(self) -> None:
        # Two competing outgoing edges from x — the walker should
        # follow the higher-confidence one.
        edges = [
            CausalEdge(src="x", dst="weak_cause", edge_type="caused_by", confidence=0.4),
            CausalEdge(src="x", dst="strong_cause", edge_type="caused_by", confidence=0.9),
        ]
        path, _ = await _walk_causal_chain("x", edges)
        assert path == ["x", "strong_cause"]

    @pytest.mark.asyncio
    async def test_enables_edge_traversed(self) -> None:
        edges = [
            CausalEdge(src="decision", dst="enabler", edge_type="enables"),
        ]
        path, edge_types = await _walk_causal_chain("decision", edges)
        assert path == ["decision", "enabler"]
        assert edge_types == ["enables"]


# ---------------------------------------------------------------------------
# _dispatch_transitive_cause integration tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeNode:
    id: str
    memory_id: str


@dataclass
class _FakeZoneEdge:
    src: str
    dst: str


class _FakeCtx:
    """Minimal _DispatchCtx stand-in that only exposes get_causal_edges()."""

    def __init__(self, causal_edges: list[CausalEdge]) -> None:
        self._causal_edges = causal_edges

    async def get_causal_edges(self) -> list[CausalEdge]:
        return self._causal_edges


class _FakeStore:
    async def get_entity_states_by_entity(self, *args, **kwargs):
        return []


async def _run_dispatch(
    subject: str,
    entity: str,
    x_node_memory_id: str,
    node_index: dict,
    zone_edges: list,
    causal_edges: list[CausalEdge],
) -> LGTrace:
    """Helper: run _dispatch_transitive_cause with mocked inputs."""
    trace = LGTrace(
        query=f"what caused {entity} in {subject}?",
        intent=LGIntent(
            kind="transitive_cause",
            subject=subject,
            entity=entity,
        ),
        confidence=Confidence.ABSTAIN,
    )
    ctx = _FakeCtx(causal_edges)
    # Monkeypatch _find_event_node where the dispatcher actually
    # resolves it — `walkers` module after the Phase F extraction
    # (used to live in `dispatch`).  Patching the dispatch alias
    # would no-op the override.
    from ncms.application.tlg import walkers as _wm

    async def _fake_find(*args, **kwargs):
        return node_index.get(x_node_memory_id)

    original = _wm._find_event_node
    _wm._find_event_node = _fake_find
    try:
        await _dispatch_transitive_cause(
            store=_FakeStore(),
            trace=trace,
            node_index=node_index,
            zone_edges=zone_edges,
            ctx=ctx,  # type: ignore[arg-type]
        )
    finally:
        _wm._find_event_node = original
    return trace


class TestTransitiveCauseDispatcher:
    @pytest.mark.asyncio
    async def test_ctlg_path_used_when_causal_edges_exist(self) -> None:
        # Query: "what caused the rewrite in auth-service?"
        x_node = _FakeNode(id="n_rewrite", memory_id="m_rewrite")
        causal_edges = [
            CausalEdge(src="m_rewrite", dst="m_outage", edge_type="caused_by", confidence=0.9),
            CausalEdge(src="m_outage", dst="m_audit", edge_type="caused_by", confidence=0.85),
        ]
        trace = await _run_dispatch(
            subject="auth-service",
            entity="rewrite",
            x_node_memory_id="n_rewrite",
            node_index={"n_rewrite": x_node},
            zone_edges=[],
            causal_edges=causal_edges,
        )
        assert trace.confidence == Confidence.HIGH
        assert trace.grammar_answer == "m_audit"
        # Proof should name the CTLG path (not the pre-CTLG fallback).
        assert "CTLG causal chain" in (trace.proof or "")

    @pytest.mark.asyncio
    async def test_fallback_when_no_causal_edges(self) -> None:
        # Query with zone edges but no causal edges — walker uses the
        # pre-CTLG timestamp-predecessor path.
        x_node = _FakeNode(id="n_rewrite", memory_id="m_rewrite")
        zone_edges = [
            _FakeZoneEdge(src="m_origin", dst="n_rewrite"),
        ]
        trace = await _run_dispatch(
            subject="auth-service",
            entity="rewrite",
            x_node_memory_id="n_rewrite",
            node_index={"n_rewrite": x_node},
            zone_edges=zone_edges,
            causal_edges=[],
        )
        assert trace.confidence == Confidence.HIGH
        assert trace.grammar_answer == "m_origin"
        assert "pre-CTLG" in (trace.proof or "")

    @pytest.mark.asyncio
    async def test_abstain_when_no_edges_at_all(self) -> None:
        x_node = _FakeNode(id="n_x", memory_id="m_x")
        trace = await _run_dispatch(
            subject="auth-service",
            entity="x",
            x_node_memory_id="n_x",
            node_index={"n_x": x_node},
            zone_edges=[],
            causal_edges=[],
        )
        assert trace.confidence == Confidence.ABSTAIN

    @pytest.mark.asyncio
    async def test_missing_subject_abstains(self) -> None:
        trace = LGTrace(
            query="what caused the rewrite",
            intent=LGIntent(
                kind="transitive_cause",
                subject=None,
                entity="rewrite",
            ),
            confidence=Confidence.ABSTAIN,
        )
        ctx = _FakeCtx([])
        await _dispatch_transitive_cause(
            store=_FakeStore(),
            trace=trace,
            node_index={},
            zone_edges=[],
            ctx=ctx,  # type: ignore[arg-type]
        )
        assert trace.confidence == Confidence.ABSTAIN
        assert "missing slots" in (trace.proof or "")
