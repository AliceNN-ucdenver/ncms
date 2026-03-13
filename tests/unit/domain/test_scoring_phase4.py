"""Unit tests for Phase 4 scoring functions (hierarchy match bonus)."""

from __future__ import annotations

from ncms.domain.scoring import hierarchy_match_bonus


class TestHierarchyMatchBonus:
    """Tests for hierarchy_match_bonus() — intent-aware type matching."""

    def test_returns_bonus_when_match(self) -> None:
        bonus = hierarchy_match_bonus(
            candidate_node_types=["entity_state"],
            target_node_types=("entity_state",),
        )
        assert bonus == 0.5  # default bonus

    def test_returns_zero_when_no_match(self) -> None:
        bonus = hierarchy_match_bonus(
            candidate_node_types=["atomic"],
            target_node_types=("episode",),
        )
        assert bonus == 0.0

    def test_returns_bonus_on_partial_match(self) -> None:
        """One of multiple candidate types matches."""
        bonus = hierarchy_match_bonus(
            candidate_node_types=["atomic", "entity_state"],
            target_node_types=("entity_state",),
        )
        assert bonus == 0.5

    def test_empty_candidate_types_returns_zero(self) -> None:
        bonus = hierarchy_match_bonus(
            candidate_node_types=[],
            target_node_types=("entity_state",),
        )
        assert bonus == 0.0

    def test_empty_target_types_returns_zero(self) -> None:
        bonus = hierarchy_match_bonus(
            candidate_node_types=["atomic"],
            target_node_types=(),
        )
        assert bonus == 0.0

    def test_custom_bonus_value(self) -> None:
        bonus = hierarchy_match_bonus(
            candidate_node_types=["episode"],
            target_node_types=("episode", "atomic"),
            bonus=1.0,
        )
        assert bonus == 1.0

    def test_multiple_targets_any_match_wins(self) -> None:
        """Intent targets multiple types — first match wins."""
        bonus = hierarchy_match_bonus(
            candidate_node_types=["atomic"],
            target_node_types=("entity_state", "atomic"),
        )
        assert bonus == 0.5
