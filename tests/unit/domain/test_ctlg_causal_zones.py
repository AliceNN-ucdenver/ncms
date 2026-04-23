"""Unit tests for CTLG causal zone builder.

Pure-function tests for :func:`build_causal_zones` and the
:class:`CausalZone` / :class:`CausalEdge` dataclasses.  These are
the building blocks the dispatcher will walk at query time.
"""

from __future__ import annotations

from ncms.domain.tlg.zones import (
    CausalEdge,
    CausalZone,
    build_causal_zones,
)


def _edge(src: str, dst: str, et: str = "caused_by") -> CausalEdge:
    return CausalEdge(src=src, dst=dst, edge_type=et)


class TestEmpty:
    def test_no_edges_returns_empty(self) -> None:
        assert build_causal_zones([]) == []


class TestDirection:
    def test_caused_by_direction_semantics(self) -> None:
        # CAUSED_BY points effect→cause.  So:
        #   "outage CAUSED_BY audit" = edge(src=outage, dst=audit)
        # audit is the root cause; outage is the leaf effect.
        edges = [_edge("outage", "audit")]
        zones = build_causal_zones(edges)
        assert len(zones) == 1
        z = zones[0]
        assert z.root_causes == ("audit",)
        assert z.leaf_effects == ("outage",)


class TestMultiLevelChain:
    def test_three_step_chain(self) -> None:
        # auth_migration CAUSED_BY outage CAUSED_BY audit
        edges = [
            _edge("auth_mig", "outage"),
            _edge("outage", "audit"),
        ]
        zones = build_causal_zones(edges)
        assert len(zones) == 1
        z = zones[0]
        assert z.member_ids == frozenset({"auth_mig", "outage", "audit"})
        # Only audit has no outgoing caused_by → root cause.
        assert z.root_causes == ("audit",)
        # Only auth_mig has no incoming caused_by → leaf effect.
        assert z.leaf_effects == ("auth_mig",)


class TestBranching:
    def test_one_cause_multiple_effects(self) -> None:
        # Both outage and vault are caused by audit.
        edges = [
            _edge("outage", "audit"),
            _edge("vault", "audit"),
        ]
        zones = build_causal_zones(edges)
        assert len(zones) == 1
        z = zones[0]
        assert z.root_causes == ("audit",)
        assert set(z.leaf_effects) == {"outage", "vault"}

    def test_multiple_causes_one_effect(self) -> None:
        # The rewrite was caused by both a scale issue and a cost issue.
        edges = [
            _edge("rewrite", "scale"),
            _edge("rewrite", "cost"),
        ]
        zones = build_causal_zones(edges)
        assert len(zones) == 1
        z = zones[0]
        assert set(z.root_causes) == {"scale", "cost"}
        assert z.leaf_effects == ("rewrite",)


class TestDisconnected:
    def test_two_independent_chains_produce_two_zones(self) -> None:
        edges = [
            _edge("a_effect", "a_cause"),
            _edge("b_effect", "b_cause"),
        ]
        zones = build_causal_zones(edges)
        assert len(zones) == 2
        members = [z.member_ids for z in zones]
        assert frozenset({"a_effect", "a_cause"}) in members
        assert frozenset({"b_effect", "b_cause"}) in members


class TestSubjectCoverage:
    def test_subject_coverage_when_provided(self) -> None:
        edges = [_edge("outage", "audit"), _edge("auth_mig", "outage")]
        subjects = {
            "outage": "payments",
            "audit": "compliance",
            "auth_mig": "auth-service",
        }
        zones = build_causal_zones(edges, subjects)
        assert len(zones) == 1
        assert zones[0].subject_coverage == frozenset({
            "payments", "compliance", "auth-service",
        })

    def test_subject_coverage_empty_when_not_provided(self) -> None:
        edges = [_edge("a", "b")]
        zones = build_causal_zones(edges)
        assert zones[0].subject_coverage == frozenset()


class TestDeterminism:
    def test_zone_id_monotonic(self) -> None:
        edges = [
            _edge("a_effect", "a_cause"),
            _edge("b_effect", "b_cause"),
        ]
        zones = build_causal_zones(edges)
        ids = [z.zone_id for z in zones]
        assert ids == sorted(ids)

    def test_roots_and_leaves_sorted(self) -> None:
        # Multiple roots + leaves — the output should be lex-sorted
        # for deterministic downstream iteration.
        edges = [
            _edge("effect", "cause_b"),
            _edge("effect", "cause_a"),
            _edge("effect_z", "cause_a"),
            _edge("effect_y", "cause_a"),
        ]
        zones = build_causal_zones(edges)
        z = zones[0]
        assert z.root_causes == tuple(sorted(z.root_causes))
        assert z.leaf_effects == tuple(sorted(z.leaf_effects))


class TestEnablesIntegration:
    def test_enables_edges_merge_into_same_zone(self) -> None:
        # ENABLES edges participate in zone formation — they're
        # treated as causal-dependent by the traversal, just with
        # different semantics (necessary vs sufficient).
        edges = [
            CausalEdge(src="postgres_decision", dst="pgvector_available",
                       edge_type="enables"),
            CausalEdge(src="postgres_decision", dst="scale_pressure",
                       edge_type="caused_by"),
        ]
        zones = build_causal_zones(edges)
        assert len(zones) == 1
        assert len(zones[0].member_ids) == 3


class TestCausalZoneSmoke:
    def test_dataclass_is_frozen(self) -> None:
        z = CausalZone(
            zone_id=0,
            member_ids=frozenset({"a"}),
            root_causes=("a",),
            leaf_effects=("a",),
        )
        try:
            z.zone_id = 99  # type: ignore[misc]
        except Exception:
            return  # expected — frozen dataclass
        raise AssertionError("CausalZone should be frozen")
