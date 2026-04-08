"""Tests for ACT-R activation scoring functions."""

import math

import pytest

from ncms.domain.scoring import (
    activation_noise,
    base_level_activation,
    retrieval_probability,
    spreading_activation,
    total_activation,
)


class TestBaseLevelActivation:
    def test_single_recent_access(self):
        """A single access should produce activation matching the formula."""
        age = 10.0
        decay = 0.5
        result = base_level_activation([age], decay=decay)
        expected = math.log(age ** (-decay))
        assert result == pytest.approx(expected, abs=1e-9)

    def test_multiple_accesses_higher_than_single(self):
        """More accesses should produce higher activation."""
        single = base_level_activation([60.0])
        multiple = base_level_activation([60.0, 120.0, 300.0])
        assert multiple > single

    def test_recent_access_higher_than_old(self):
        """Recent access should give higher activation than old access."""
        recent = base_level_activation([10.0])
        old = base_level_activation([100000.0])
        assert recent > old

    def test_empty_accesses_returns_minimum(self):
        """No accesses should return very low (floor) activation."""
        result = base_level_activation([])
        # Should be deeply negative - the exact sentinel doesn't matter,
        # just that it's much lower than any real activation
        assert result < -5.0

    def test_zero_age_is_excluded(self):
        """Zero-age accesses should be ignored (would cause division by zero)."""
        result = base_level_activation([0.0, 10.0])
        expected = base_level_activation([10.0])
        assert result == expected

    def test_all_zero_ages_returns_minimum(self):
        """If all ages are zero, result should be the minimum floor."""
        result = base_level_activation([0.0, 0.0])
        empty_result = base_level_activation([])
        assert result == empty_result

    def test_custom_decay_parameter(self):
        """Lower decay means slower forgetting, so higher activation."""
        age = 100.0
        low_decay = base_level_activation([age], decay=0.3)
        high_decay = base_level_activation([age], decay=0.7)
        assert low_decay > high_decay

    def test_formula_with_multiple_accesses(self):
        """Multiple accesses should match sum formula: ln(sum(t_j^-d))."""
        ages = [5.0, 15.0, 45.0]
        decay = 0.5
        result = base_level_activation(ages, decay=decay)
        expected = math.log(sum(t ** (-decay) for t in ages))
        assert result == pytest.approx(expected, abs=1e-9)

    def test_monotonic_with_additional_accesses(self):
        """Adding more accesses should never decrease activation."""
        ages_short = [30.0, 60.0]
        ages_long = [30.0, 60.0, 90.0, 120.0]
        assert base_level_activation(ages_long) >= base_level_activation(ages_short)


class TestSpreadingActivation:
    def test_full_overlap(self):
        """Complete overlap should give maximum spreading activation."""
        entities = ["a", "b", "c"]
        result = spreading_activation(
            memory_entity_ids=entities,
            context_entity_ids=entities,
        )
        assert result > 0

    def test_no_overlap(self):
        """No overlap should give zero spreading activation."""
        result = spreading_activation(
            memory_entity_ids=["a", "b"],
            context_entity_ids=["c", "d"],
        )
        assert result == 0.0

    def test_partial_overlap_is_intermediate(self):
        """Partial overlap should be between zero and full overlap."""
        full = spreading_activation(["a", "b"], ["a", "b"])
        partial = spreading_activation(["a", "b"], ["a", "c"])
        assert 0 < partial < full

    def test_empty_memory_entities(self):
        """Empty memory entities should give zero."""
        assert spreading_activation([], ["a", "b"]) == 0.0

    def test_empty_context_entities(self):
        """Empty context entities should give zero."""
        assert spreading_activation(["a", "b"], []) == 0.0

    def test_custom_source_activation(self):
        """Higher source activation should produce proportionally higher result."""
        low = spreading_activation(["a"], ["a"], source_activation=1.0)
        high = spreading_activation(["a"], ["a"], source_activation=2.0)
        assert high == pytest.approx(low * 2, abs=1e-9)

    def test_with_association_strengths(self):
        """Explicit association strengths should weight the overlap."""
        strengths = {("a", "a"): 0.5}
        result = spreading_activation(
            memory_entity_ids=["a"],
            context_entity_ids=["a"],
            association_strengths=strengths,
        )
        assert result > 0

    def test_more_overlap_same_context_gives_more_activation(self):
        """With same context, more matching memory entities give higher activation."""
        # Same context ["a", "b"], but one memory has only 1 match vs 2 matches
        fewer = spreading_activation(["a", "c"], ["a", "b"])   # 1 overlap out of 2 context
        more = spreading_activation(["a", "b"], ["a", "b"])    # 2 overlaps out of 2 context
        assert more > fewer


class TestActivationNoise:
    def test_zero_sigma_is_deterministic(self):
        """Zero noise should always return 0."""
        for _ in range(10):
            assert activation_noise(sigma=0.0) == 0.0

    def test_positive_sigma_returns_finite(self):
        """Positive sigma should return a finite number."""
        for _ in range(20):
            result = activation_noise(sigma=0.25)
            assert math.isfinite(result)

    def test_noise_has_variance(self):
        """With positive sigma, repeated calls should produce varying values."""
        values = [activation_noise(sigma=0.5) for _ in range(50)]
        unique = set(values)
        # Should not all be the same value
        assert len(unique) > 1

    def test_negative_sigma_treated_as_zero(self):
        """Negative sigma should produce zero noise."""
        assert activation_noise(sigma=-0.5) == 0.0


class TestTotalActivation:
    def test_components_sum(self):
        """Total activation should equal base + spreading + noise."""
        base, spreading, noise = 2.0, 1.0, 0.5
        result = total_activation(base_level=base, spreading=spreading, noise=noise)
        assert result == pytest.approx(base + spreading + noise, abs=1e-9)

    def test_mismatch_penalty_subtracts(self):
        """Mismatch penalty should be subtracted from total."""
        base, penalty = 2.0, 0.5
        result = total_activation(base_level=base, mismatch_penalty=penalty)
        assert result == pytest.approx(base - penalty, abs=1e-9)

    def test_all_components_combined(self):
        """All components should combine correctly."""
        base, spread, noise, penalty = 3.0, 1.5, 0.2, 0.7
        result = total_activation(
            base_level=base, spreading=spread, noise=noise, mismatch_penalty=penalty,
        )
        expected = base + spread + noise - penalty
        assert result == pytest.approx(expected, abs=1e-9)

    def test_defaults_only_base_level(self):
        """With only base level, other components default to zero."""
        base = 1.5
        result = total_activation(base_level=base)
        assert result == pytest.approx(base, abs=1e-9)


class TestRetrievalProbability:
    def test_high_activation_gives_high_probability(self):
        """Well above threshold should give probability near 1."""
        p = retrieval_probability(activation=5.0, threshold=-2.0)
        assert p > 0.99

    def test_low_activation_gives_low_probability(self):
        """Well below threshold should give probability near 0."""
        p = retrieval_probability(activation=-10.0, threshold=-2.0)
        assert p < 0.01

    def test_at_threshold_gives_half(self):
        """At exactly the threshold, probability should be 0.5."""
        threshold = -2.0
        p = retrieval_probability(activation=threshold, threshold=threshold)
        assert p == pytest.approx(0.5, abs=1e-9)

    def test_monotonic(self):
        """Higher activation should always give higher probability."""
        threshold = -2.0
        p_low = retrieval_probability(activation=-1.0, threshold=threshold)
        p_high = retrieval_probability(activation=2.0, threshold=threshold)
        assert p_high > p_low

    def test_probability_bounded(self):
        """Result should always be in [0, 1]."""
        for act in [-100.0, -10.0, 0.0, 10.0, 100.0]:
            p = retrieval_probability(activation=act)
            assert 0.0 <= p <= 1.0

    def test_custom_tau(self):
        """Smaller tau should make the transition sharper."""
        threshold = 0.0
        activation = 0.5
        # Sharper tau -> closer to 1.0 when above threshold
        sharp = retrieval_probability(activation=activation, threshold=threshold, tau=0.1)
        soft = retrieval_probability(activation=activation, threshold=threshold, tau=1.0)
        assert sharp > soft
