"""Tests for admission scoring functions: AdmissionFeatures, score_admission, route_memory."""

import pytest

from ncms.domain.scoring import (
    ADMISSION_WEIGHTS,
    AdmissionFeatures,
    route_memory,
    score_admission,
)


class TestAdmissionFeatures:
    def test_defaults_all_zero(self):
        f = AdmissionFeatures()
        assert f.novelty == 0.0
        assert f.utility == 0.0
        assert f.reliability == 0.0
        assert f.temporal_salience == 0.0
        assert f.persistence == 0.0
        assert f.redundancy == 0.0
        assert f.episode_affinity == 0.0
        assert f.state_change_signal == 0.0

    def test_frozen(self):
        """AdmissionFeatures is immutable."""
        f = AdmissionFeatures(novelty=0.5)
        with pytest.raises(AttributeError):
            f.novelty = 0.9  # type: ignore[misc]

    def test_all_fields_set(self):
        f = AdmissionFeatures(
            novelty=0.8,
            utility=0.6,
            reliability=0.9,
            temporal_salience=0.5,
            persistence=0.7,
            redundancy=0.2,
            episode_affinity=0.3,
            state_change_signal=0.1,
        )
        assert f.novelty == 0.8
        assert f.redundancy == 0.2


class TestScoreAdmission:
    def test_all_zeros(self):
        """Zero features → zero score."""
        assert score_admission(AdmissionFeatures()) == 0.0

    def test_all_ones_no_redundancy(self):
        """All features at 1.0 with zero redundancy → sum of positive weights."""
        f = AdmissionFeatures(
            novelty=1.0,
            utility=1.0,
            reliability=1.0,
            temporal_salience=1.0,
            persistence=1.0,
            redundancy=0.0,
            episode_affinity=1.0,
            state_change_signal=1.0,
        )
        expected = 0.20 + 0.18 + 0.12 + 0.12 + 0.15 + 0.04 + 0.14
        assert score_admission(f) == pytest.approx(expected, abs=1e-9)

    def test_full_redundancy_lowers_score(self):
        """High redundancy should reduce the score."""
        base = AdmissionFeatures(novelty=0.5, reliability=0.5)
        redundant = AdmissionFeatures(novelty=0.5, reliability=0.5, redundancy=1.0)
        assert score_admission(redundant) < score_admission(base)

    def test_redundancy_weight_is_negative(self):
        """Redundancy is the only negative weight in the formula."""
        assert ADMISSION_WEIGHTS["redundancy"] < 0
        positive_weights = {k: v for k, v in ADMISSION_WEIGHTS.items() if k != "redundancy"}
        assert all(v > 0 for v in positive_weights.values())

    def test_known_vector(self):
        """Manual computation of a known feature vector."""
        f = AdmissionFeatures(
            novelty=0.8,
            utility=0.6,
            reliability=0.7,
            temporal_salience=0.3,
            persistence=0.5,
            redundancy=0.1,
            episode_affinity=0.0,
            state_change_signal=0.4,
        )
        expected = (
            0.20 * 0.8
            + 0.18 * 0.6
            + 0.12 * 0.7
            + 0.12 * 0.3
            + 0.15 * 0.5
            - 0.15 * 0.1
            + 0.04 * 0.0
            + 0.14 * 0.4
        )
        assert score_admission(f) == pytest.approx(expected, abs=1e-9)

    def test_weights_sum_to_one(self):
        """Absolute values of weights should sum to ~1.10 (per spec)."""
        total = sum(abs(v) for v in ADMISSION_WEIGHTS.values())
        # 0.20+0.18+0.12+0.12+0.15+0.15+0.04+0.14 = 1.10
        assert total == pytest.approx(1.10, abs=0.01)


class TestRouteMemory:
    def test_discard_low_score(self):
        """Low score + low persistence + low state_change → discard."""
        f = AdmissionFeatures(
            novelty=0.1,
            persistence=0.10,
            state_change_signal=0.05,
        )
        score = score_admission(f)
        assert score < 0.25
        assert route_memory(f, score) == "discard"

    def test_ephemeral_cache(self):
        """Mid-range score → ephemeral_cache."""
        f = AdmissionFeatures(
            novelty=0.5,
            utility=0.4,
            reliability=0.5,
            persistence=0.30,
        )
        score = score_admission(f)
        # Should be in range [0.25, 0.35)
        assert 0.25 <= score < 0.35
        assert route_memory(f, score) == "ephemeral_cache"

    def test_persist_high_score(self):
        """High score → persist (quality gate passed)."""
        f = AdmissionFeatures(
            novelty=0.9,
            utility=0.8,
            reliability=0.8,
            temporal_salience=0.5,
            persistence=0.7,
            redundancy=0.1,
        )
        score = score_admission(f)
        assert score >= 0.35
        assert route_memory(f, score) == "persist"

    def test_high_state_change_persists(self):
        """High state_change_signal routes to persist (L2 created downstream)."""
        f = AdmissionFeatures(
            novelty=0.3,
            state_change_signal=0.55,
        )
        score = score_admission(f)
        # State change is a classification signal, not a routing destination
        assert route_memory(f, score) == "persist"

    def test_high_episode_affinity_persists(self):
        """High episode_affinity routes to persist (episode linking done downstream)."""
        f = AdmissionFeatures(
            novelty=0.8,
            utility=0.5,
            reliability=0.6,
            persistence=0.5,
            episode_affinity=0.60,
        )
        score = score_admission(f)
        assert score >= 0.35
        assert route_memory(f, score) == "persist"

    def test_all_routes_reachable(self):
        """Verify all 3 routes can be produced."""
        routes = set()

        # discard
        f = AdmissionFeatures(novelty=0.05, persistence=0.05, state_change_signal=0.05)
        routes.add(route_memory(f, 0.10))

        # ephemeral_cache (range: 0.25 <= score < 0.35)
        f = AdmissionFeatures(novelty=0.5)
        routes.add(route_memory(f, 0.30))

        # persist (score >= 0.35)
        f = AdmissionFeatures(novelty=0.9)
        routes.add(route_memory(f, 0.60))

        assert routes == {"discard", "ephemeral_cache", "persist"}
