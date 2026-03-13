"""Tests for Phase 2C scoring penalty functions."""

from __future__ import annotations

from ncms.domain.scoring import (
    conflict_annotation_penalty,
    supersession_penalty,
    total_activation,
)


class TestSupersessionPenalty:
    def test_returns_penalty_when_superseded(self) -> None:
        assert supersession_penalty(True) == 0.3

    def test_returns_zero_when_current(self) -> None:
        assert supersession_penalty(False) == 0.0

    def test_custom_penalty_value(self) -> None:
        assert supersession_penalty(True, penalty=0.5) == 0.5

    def test_zero_penalty_value(self) -> None:
        assert supersession_penalty(True, penalty=0.0) == 0.0


class TestConflictAnnotationPenalty:
    def test_returns_penalty_when_has_conflicts(self) -> None:
        assert conflict_annotation_penalty(True) == 0.15

    def test_returns_zero_when_no_conflicts(self) -> None:
        assert conflict_annotation_penalty(False) == 0.0

    def test_custom_penalty_value(self) -> None:
        assert conflict_annotation_penalty(True, penalty=0.25) == 0.25


class TestTotalActivationWithPenalty:
    def test_penalty_reduces_activation(self) -> None:
        """Activation with penalty should be lower than without."""
        base = total_activation(1.0, 0.5, 0.0, mismatch_penalty=0.0)
        penalized = total_activation(1.0, 0.5, 0.0, mismatch_penalty=0.3)
        assert penalized < base
        assert penalized == base - 0.3

    def test_both_penalties_stack(self) -> None:
        """Supersession + conflict penalties should stack."""
        penalty = supersession_penalty(True, 0.3) + conflict_annotation_penalty(True, 0.15)
        assert abs(penalty - 0.45) < 1e-10
        act = total_activation(2.0, 0.0, 0.0, mismatch_penalty=penalty)
        assert abs(act - (2.0 - 0.45)) < 1e-10

    def test_zero_penalty_no_effect(self) -> None:
        act_no_penalty = total_activation(1.5, 0.3, 0.0)
        act_zero_penalty = total_activation(1.5, 0.3, 0.0, mismatch_penalty=0.0)
        assert act_no_penalty == act_zero_penalty
