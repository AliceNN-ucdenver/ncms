"""Tests for admission scoring functions: AdmissionFeatures, score_admission, route_memory.

score_admission uses 4 features with weights:
    utility (0.30), persistence (0.25), state_change_signal (0.25), temporal_salience (0.20)
"""

import pytest

from ncms.domain.scoring import (
    AdmissionFeatures,
    route_memory,
    score_admission,
)


class TestAdmissionFeatures:
    def test_defaults_all_zero(self):
        f = AdmissionFeatures()
        assert f.utility == 0.0
        assert f.temporal_salience == 0.0
        assert f.persistence == 0.0
        assert f.state_change_signal == 0.0

    def test_frozen(self):
        """AdmissionFeatures is immutable."""
        f = AdmissionFeatures(utility=0.5)
        with pytest.raises(AttributeError):
            f.utility = 0.9  # type: ignore[misc]

    def test_all_fields_set(self):
        f = AdmissionFeatures(
            utility=0.6,
            temporal_salience=0.5,
            persistence=0.7,
            state_change_signal=0.1,
        )
        assert f.utility == 0.6
        assert f.persistence == 0.7


class TestScoreAdmission:
    def test_all_zeros(self):
        """Zero features → zero score."""
        assert score_admission(AdmissionFeatures()) == 0.0

    def test_all_features_at_one(self):
        """All features at 1.0 → weights sum to 1.0."""
        f = AdmissionFeatures(
            utility=1.0,
            temporal_salience=1.0,
            persistence=1.0,
            state_change_signal=1.0,
        )
        expected = 0.30 + 0.25 + 0.25 + 0.20  # = 1.0
        assert score_admission(f) == pytest.approx(expected, abs=1e-9)

    def test_known_vector(self):
        """Manual computation of a known feature vector."""
        f = AdmissionFeatures(
            utility=0.6,
            temporal_salience=0.3,
            persistence=0.5,
            state_change_signal=0.4,
        )
        expected = 0.30 * 0.6 + 0.25 * 0.5 + 0.25 * 0.4 + 0.20 * 0.3
        assert score_admission(f) == pytest.approx(expected, abs=1e-9)

    def test_weights_sum_to_one(self):
        """Weights sum to 1.0."""
        active_weights = [0.30, 0.25, 0.25, 0.20]
        assert sum(active_weights) == pytest.approx(1.0, abs=0.01)


class TestRouteMemory:
    def test_discard_low_score(self):
        """Low score + low state_change → discard."""
        f = AdmissionFeatures(
            persistence=0.10,
            state_change_signal=0.05,
        )
        score = score_admission(f)
        assert score < 0.10
        assert route_memory(f, score) == "discard"

    def test_ephemeral_cache(self):
        """Mid-range score → ephemeral_cache."""
        f = AdmissionFeatures(
            utility=0.4,
            persistence=0.30,
        )
        score = score_admission(f)
        # 0.30*0.4 + 0.25*0.3 = 0.12 + 0.075 = 0.195
        assert 0.10 <= score < 0.25
        assert route_memory(f, score) == "ephemeral_cache"

    def test_persist_high_score(self):
        """High score → persist (quality gate passed)."""
        f = AdmissionFeatures(
            utility=0.8,
            temporal_salience=0.5,
            persistence=0.7,
            state_change_signal=0.3,
        )
        score = score_admission(f)
        assert score >= 0.25
        assert route_memory(f, score) == "persist"

    def test_high_state_change_persists(self):
        """High state_change_signal routes to persist regardless of score."""
        f = AdmissionFeatures(
            state_change_signal=0.55,
        )
        score = score_admission(f)
        assert route_memory(f, score) == "persist"

    def test_state_change_threshold(self):
        """state_change_signal below 0.35 does not auto-promote."""
        f = AdmissionFeatures(
            state_change_signal=0.30,
        )
        score = score_admission(f)
        # 0.25 * 0.30 = 0.075 — below 0.10 threshold → discard
        assert route_memory(f, score) == "discard"

    def test_monotonic_routing(self):
        """Higher score always routes to same or better tier.

        discard < ephemeral_cache < persist (monotonic).
        """
        tier_rank = {"discard": 0, "ephemeral_cache": 1, "persist": 2}
        f = AdmissionFeatures()  # state_change_signal=0 so no auto-promote

        prev_rank = -1
        for score in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.50, 0.80, 1.0]:
            route = route_memory(f, score)
            rank = tier_rank[route]
            assert rank >= prev_rank, (
                f"score={score} routed to {route} (rank {rank}) < prev {prev_rank}"
            )
            prev_rank = rank

    def test_all_routes_reachable(self):
        """Verify all 3 routes can be produced."""
        routes = set()

        # discard (score < 0.10)
        f = AdmissionFeatures()
        routes.add(route_memory(f, 0.05))

        # ephemeral_cache (0.10 <= score < 0.25)
        routes.add(route_memory(f, 0.15))

        # persist (score >= 0.25)
        routes.add(route_memory(f, 0.50))

        assert routes == {"discard", "ephemeral_cache", "persist"}
