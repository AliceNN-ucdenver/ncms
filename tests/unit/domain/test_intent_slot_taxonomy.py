"""Tests for the intent-slot taxonomy domain helpers.

Currently focused on :func:`slm_state_change_decision` — the Phase I.2
disciplined retirement gate that decides whether the SLM has
confidently classified state-change for a given memory.
"""

from __future__ import annotations

from ncms.domain.intent_slot_taxonomy import slm_state_change_decision


class TestSlmStateChangeDecision:
    """Tests for :func:`slm_state_change_decision`.

    The helper returns:
      * ``(has_state_change, has_state_declaration)`` when the SLM ran
        confidently — caller MUST trust the verdict (including
        ``"none"``) and skip the regex fallback.
      * ``None`` when SLM didn't run confidently — caller falls back
        to regex/heuristic detection.
    """

    THRESHOLD = 0.3

    def test_declaration_above_threshold_returns_true_change(self) -> None:
        result = slm_state_change_decision(
            {
                "method": "joint_bert_lora",
                "state_change": "declaration",
                "state_change_confidence": 0.9,
            },
            threshold=self.THRESHOLD,
        )
        assert result == (True, False)

    def test_retirement_above_threshold_returns_true_change(self) -> None:
        result = slm_state_change_decision(
            {
                "method": "joint_bert_lora",
                "state_change": "retirement",
                "state_change_confidence": 0.85,
            },
            threshold=self.THRESHOLD,
        )
        assert result == (True, False)

    def test_none_above_threshold_returns_false_change_not_none(self) -> None:
        """The bug fix: 'none' from SLM means NO state change, NOT 'try regex'.

        Previously the code path treated state_change='none' as an
        invitation to fall through to regex detection, which let
        spurious YAML/status patterns create L2 nodes the SLM had
        explicitly classified as not-a-state-change.  This is the
        canonical regression test for that retirement.
        """
        result = slm_state_change_decision(
            {
                "method": "joint_bert_lora",
                "state_change": "none",
                "state_change_confidence": 0.95,
            },
            threshold=self.THRESHOLD,
        )
        assert result == (False, False)

    def test_below_threshold_returns_none(self) -> None:
        """Low-confidence SLM output → fall back to regex."""
        result = slm_state_change_decision(
            {
                "method": "joint_bert_lora",
                "state_change": "declaration",
                "state_change_confidence": 0.15,
            },
            threshold=self.THRESHOLD,
        )
        assert result is None

    def test_e5_zero_shot_method_returns_none(self) -> None:
        """E5 zero-shot doesn't emit state_change → fall back to regex."""
        result = slm_state_change_decision(
            {
                "method": "e5_zero_shot",
                "state_change": None,
                "state_change_confidence": None,
            },
            threshold=self.THRESHOLD,
        )
        assert result is None

    def test_heuristic_fallback_method_returns_none(self) -> None:
        """Heuristic fallback emits no state_change → regex needed."""
        result = slm_state_change_decision(
            {
                "method": "heuristic_fallback",
                "state_change": None,
            },
            threshold=self.THRESHOLD,
        )
        assert result is None

    def test_empty_dict_returns_none(self) -> None:
        assert slm_state_change_decision(
            {}, threshold=self.THRESHOLD,
        ) is None

    def test_none_input_returns_none(self) -> None:
        assert slm_state_change_decision(
            None, threshold=self.THRESHOLD,
        ) is None

    def test_missing_state_change_returns_none(self) -> None:
        """Method matches but no state_change field → fall through."""
        result = slm_state_change_decision(
            {
                "method": "joint_bert_lora",
                "state_change_confidence": 0.9,
            },
            threshold=self.THRESHOLD,
        )
        assert result is None

    def test_missing_confidence_treated_as_zero(self) -> None:
        """Missing confidence falls below any positive threshold."""
        result = slm_state_change_decision(
            {
                "method": "joint_bert_lora",
                "state_change": "declaration",
            },
            threshold=0.1,
        )
        assert result is None

    def test_threshold_boundary_inclusive(self) -> None:
        """Confidence == threshold is accepted (>=, not >)."""
        result = slm_state_change_decision(
            {
                "method": "joint_bert_lora",
                "state_change": "declaration",
                "state_change_confidence": 0.3,
            },
            threshold=0.3,
        )
        assert result == (True, False)

    def test_unrecognized_state_label_returns_false(self) -> None:
        """An unexpected state_change value (not declaration/retirement/none)
        is treated as 'no state change' — has_change=False.  Defensive
        against future corpus drift or backend bugs.
        """
        result = slm_state_change_decision(
            {
                "method": "joint_bert_lora",
                "state_change": "weird_new_label",
                "state_change_confidence": 0.95,
            },
            threshold=self.THRESHOLD,
        )
        assert result == (False, False)

    def test_custom_primary_method(self) -> None:
        """Future SLM backends with different method names work via override."""
        result = slm_state_change_decision(
            {
                "method": "future_v10_classifier",
                "state_change": "declaration",
                "state_change_confidence": 0.9,
            },
            threshold=self.THRESHOLD,
            primary_method="future_v10_classifier",
        )
        assert result == (True, False)
