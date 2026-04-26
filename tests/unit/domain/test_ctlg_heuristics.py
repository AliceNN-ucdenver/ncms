"""Unit tests for CTLG causal heuristics.

Pure-function tests — no torch, no LLM, no I/O.  Each heuristic
gets direct tests for edge cases + monotonicity + normalization
properties.  :func:`rank_trajectories` and the default weight
registry get integration tests against the 18 TLGRelation values.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from ncms.domain.tlg.heuristics import (
    HeuristicContext,
    Trajectory,
    h_counterfactual_dist,
    h_explanatory,
    h_parsimony,
    h_recency,
    h_robustness,
    rank_trajectories,
    score_trajectory,
    weights_for_relation,
)


def _mk(**overrides):
    """Default Trajectory factory for tests."""
    defaults = dict(
        kind="refines_chain",
        memory_ids=("m1", "m2"),
        edge_types=("refines",),
        subject="auth",
    )
    defaults.update(overrides)
    return Trajectory(**defaults)


class TestExplanatory:
    def test_fully_explained_returns_one(self) -> None:
        t = _mk(explained_state_keys=frozenset({("a", "s1"), ("a", "s2"), ("a", "s3")}))
        ctx = HeuristicContext(total_state_keys=3)
        assert h_explanatory(t, ctx) == 1.0

    def test_nothing_explained_returns_zero(self) -> None:
        t = _mk(explained_state_keys=frozenset())
        ctx = HeuristicContext(total_state_keys=5)
        assert h_explanatory(t, ctx) == 0.0

    def test_partial_coverage_scales(self) -> None:
        t = _mk(explained_state_keys=frozenset({("a", "s1"), ("a", "s2")}))
        ctx = HeuristicContext(total_state_keys=4)
        assert h_explanatory(t, ctx) == pytest.approx(0.5)

    def test_capped_at_one_when_total_is_zero(self) -> None:
        t = _mk(explained_state_keys=frozenset({("a", "s1")}))
        ctx = HeuristicContext(total_state_keys=0)
        assert h_explanatory(t, ctx) == 0.0


class TestParsimony:
    def test_exact_minimum_length_returns_one(self) -> None:
        t = _mk(edge_types=("caused_by",))  # length 1
        ctx = HeuristicContext(min_length=1, parsimony_alpha=0.2)
        assert h_parsimony(t, ctx) == 1.0

    def test_below_minimum_also_one(self) -> None:
        # Trajectory shorter than min_length — excess clamped to 0.
        t = _mk(edge_types=())  # length 0
        ctx = HeuristicContext(min_length=1, parsimony_alpha=0.2)
        assert h_parsimony(t, ctx) == 1.0

    def test_excess_length_degrades_score(self) -> None:
        short = _mk(edge_types=("r",))
        long = _mk(edge_types=("r",) * 10)
        ctx = HeuristicContext(min_length=1, parsimony_alpha=0.2)
        s_short = h_parsimony(short, ctx)
        s_long = h_parsimony(long, ctx)
        assert s_short > s_long > 0.0
        assert s_long == pytest.approx(1.0 / (1.0 + 0.2 * 9))


class TestRecency:
    def test_terminal_is_now_returns_one(self) -> None:
        now = datetime.now(UTC)
        t = _mk(terminal_observed_at=now)
        ctx = HeuristicContext(
            evaluated_at=now,
            recency_lambda_per_day=0.01,
        )
        assert h_recency(t, ctx) == pytest.approx(1.0)

    def test_no_observed_at_returns_zero(self) -> None:
        t = _mk(terminal_observed_at=None)
        ctx = HeuristicContext(
            evaluated_at=datetime.now(UTC),
            recency_lambda_per_day=0.01,
        )
        assert h_recency(t, ctx) == 0.0

    def test_old_terminal_decays(self) -> None:
        now = datetime(2026, 4, 23, tzinfo=UTC)
        old = now - timedelta(days=100)
        t = _mk(terminal_observed_at=old)
        ctx = HeuristicContext(
            evaluated_at=now,
            recency_lambda_per_day=0.01,
        )
        # e^(-0.01 * 100) ≈ 0.367
        assert h_recency(t, ctx) == pytest.approx(math.exp(-1.0), rel=1e-3)

    def test_tz_naive_terminal_handled(self) -> None:
        # Trajectory has naive datetime → shouldn't crash; treated as UTC.
        now = datetime.now(UTC)
        naive = datetime(2026, 4, 1)  # tz-naive
        t = _mk(terminal_observed_at=naive)
        ctx = HeuristicContext(evaluated_at=now)
        # Just verify it doesn't raise and returns a valid float.
        s = h_recency(t, ctx)
        assert 0.0 <= s <= 1.0


class TestRobustness:
    def test_no_supports_returns_zero(self) -> None:
        t = _mk(supports_edge_counts=())
        ctx = HeuristicContext()
        assert h_robustness(t, ctx) == 0.0

    def test_one_support_per_node_returns_one(self) -> None:
        t = _mk(memory_ids=("m1", "m2"), supports_edge_counts=(1, 1))
        assert h_robustness(t, HeuristicContext()) == 1.0

    def test_partial_supports(self) -> None:
        t = _mk(memory_ids=("m1", "m2", "m3"), supports_edge_counts=(2, 0, 1))
        # total = 3 supports over 3 nodes → 1.0 (capped)
        assert h_robustness(t, HeuristicContext()) == 1.0

    def test_low_support_density(self) -> None:
        t = _mk(memory_ids=("m1", "m2", "m3", "m4"), supports_edge_counts=(1, 0, 0, 0))
        assert h_robustness(t, HeuristicContext()) == pytest.approx(0.25)


class TestCounterfactualDist:
    def test_no_skips_returns_one(self) -> None:
        t = _mk(skipped_edges=(), edge_types=("s", "s", "s"))
        assert h_counterfactual_dist(t, HeuristicContext()) == 1.0

    def test_length_zero_returns_one(self) -> None:
        t = _mk(edge_types=(), skipped_edges=())
        assert h_counterfactual_dist(t, HeuristicContext()) == 1.0

    def test_one_skip_on_four_edges(self) -> None:
        t = _mk(edge_types=("s",) * 4, skipped_edges=("e1",))
        # 1 - 1/4 = 0.75
        assert h_counterfactual_dist(t, HeuristicContext()) == pytest.approx(0.75)

    def test_all_edges_skipped_returns_zero(self) -> None:
        t = _mk(edge_types=("s", "s"), skipped_edges=("e1", "e2"))
        assert h_counterfactual_dist(t, HeuristicContext()) == 0.0


class TestScoreTrajectory:
    def test_populates_all_five_by_default(self) -> None:
        t = _mk(
            explained_state_keys=frozenset({("a", "s1")}),
            supports_edge_counts=(1, 1),
            terminal_observed_at=datetime.now(UTC),
        )
        ctx = HeuristicContext(total_state_keys=1)
        scored = score_trajectory(t, ctx)
        for h in [
            "h_explanatory",
            "h_parsimony",
            "h_recency",
            "h_robustness",
            "h_counterfactual_dist",
        ]:
            assert h in scored.heuristic_scores, f"missing {h}"

    def test_subset_scoring(self) -> None:
        t = _mk()
        ctx = HeuristicContext()
        scored = score_trajectory(t, ctx, heuristics=["h_parsimony"])
        assert list(scored.heuristic_scores.keys()) == ["h_parsimony"]

    def test_does_not_mutate_input(self) -> None:
        t = _mk()
        ctx = HeuristicContext()
        _ = score_trajectory(t, ctx)
        assert t.heuristic_scores == {}


class TestRankTrajectories:
    def test_higher_weighted_sum_ranks_first(self) -> None:
        a = _mk(
            memory_ids=("a1", "a2"),
            heuristic_scores={"h_parsimony": 0.8, "h_recency": 0.3},
        )
        b = _mk(
            memory_ids=("b1", "b2"),
            heuristic_scores={"h_parsimony": 0.5, "h_recency": 0.9},
        )
        # Weight recency heavily → b wins
        weights = {"h_parsimony": 0.2, "h_recency": 0.8}
        ranked = rank_trajectories([a, b], weights, context=HeuristicContext())
        assert ranked[0].memory_ids == ("b1", "b2")
        assert ranked[1].memory_ids == ("a1", "a2")

    def test_tie_breaks_deterministically_by_memory_id(self) -> None:
        a = _mk(
            memory_ids=("aaa",),
            edge_types=(),
            heuristic_scores={"h_parsimony": 0.5},
        )
        b = _mk(
            memory_ids=("zzz",),
            edge_types=(),
            heuristic_scores={"h_parsimony": 0.5},
        )
        weights = {"h_parsimony": 1.0}
        ranked = rank_trajectories([a, b], weights, context=HeuristicContext())
        # Tie-break is lexicographic DESC → zzz wins
        assert ranked[0].memory_ids == ("zzz",)

    def test_missing_scores_contribute_zero(self) -> None:
        a = _mk(
            memory_ids=("a",),
            edge_types=(),
            heuristic_scores={"h_parsimony": 0.9},
        )
        # Ranking asks for h_explanatory too — a doesn't have it, so 0.
        weights = {"h_parsimony": 0.5, "h_explanatory": 0.5}
        ranked = rank_trajectories([a], weights, context=HeuristicContext())
        assert ranked == [a]


class TestWeightsRegistry:
    def test_every_tlgrelation_has_default_or_fallback(self) -> None:
        # The 18 relations defined in semantic_parser.TLGRelation should
        # all resolve to something — either a registered default or
        # the fallback.
        import typing

        from ncms.domain.tlg.semantic_parser import TLGRelation

        relations = typing.get_args(TLGRelation)
        for rel in relations:
            w = weights_for_relation(rel)
            assert isinstance(w, dict)
            assert sum(w.values()) > 0, f"{rel!r} has zero total weight"

    def test_before_named_omits_recency(self) -> None:
        # "before_named" is anti-recent by design.
        w = weights_for_relation("before_named")
        assert "h_recency" not in w

    def test_first_omits_recency(self) -> None:
        w = weights_for_relation("first")
        assert "h_recency" not in w

    def test_would_be_current_if_prioritizes_counterfactual(self) -> None:
        w = weights_for_relation("would_be_current_if")
        assert w["h_counterfactual_dist"] >= 0.5

    def test_unknown_relation_falls_back(self) -> None:
        w = weights_for_relation("not_a_real_relation")
        assert w == {"h_explanatory": 0.5, "h_parsimony": 0.5}


class TestIntegration:
    def test_causal_query_ranks_by_explanatory(self) -> None:
        # Simulate a cause_of query: trajectory A explains 3 state keys
        # (0.75 of universe), B explains 1 (0.25).  With default cause_of
        # weights (h_explanatory 0.5 + h_parsimony 0.3 + h_robustness 0.2),
        # A should win.
        ctx = HeuristicContext(total_state_keys=4, min_length=1)
        a = _mk(
            memory_ids=("a1", "a2"),
            edge_types=("caused_by",),
            kind="causal_chain",
            explained_state_keys=frozenset({("s", "a"), ("s", "b"), ("s", "c")}),
            supports_edge_counts=(1, 1),
        )
        b = _mk(
            memory_ids=("b1", "b2"),
            edge_types=("caused_by",),
            kind="causal_chain",
            explained_state_keys=frozenset({("s", "x")}),
            supports_edge_counts=(0, 0),
        )
        a_scored = score_trajectory(a, ctx)
        b_scored = score_trajectory(b, ctx)
        weights = weights_for_relation("cause_of")
        ranked = rank_trajectories([b_scored, a_scored], weights, context=ctx)
        assert ranked[0].memory_ids == ("a1", "a2")
