"""Unit tests for Phase 4 scoring functions (hierarchy match bonus).

Phase H.1 added :func:`intent_alignment_bonus`, the second member of
the intent-aware bonus family.  It lives here next to its sibling so
the calibration assumption (both default to ``0.5`` raw, applied
additively to ``combined``) is testable in one place.
"""

from __future__ import annotations

from ncms.domain.scoring import (
    hierarchy_match_bonus,
    intent_alignment_bonus,
)


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


class TestIntentAlignmentBonus:
    """Tests for intent_alignment_bonus() — Phase H.1.

    The function is the pure scoring primitive: given a memory's
    SLM-emitted preference-intent label and the set of labels the
    classified QueryIntent considers aligned, apply ``bonus`` on
    match, zero otherwise.  The QueryIntent → aligned-set mapping
    itself lives at the call site (``ScoringPipeline``); these
    tests pin only the primitive's contract.
    """

    def test_returns_bonus_on_match(self) -> None:
        bonus = intent_alignment_bonus(
            memory_intent="habitual",
            aligned_intents=frozenset({"habitual"}),
        )
        assert bonus == 0.5

    def test_returns_zero_on_miss(self) -> None:
        bonus = intent_alignment_bonus(
            memory_intent="positive",
            aligned_intents=frozenset({"habitual"}),
        )
        assert bonus == 0.0

    def test_returns_zero_when_memory_intent_none(self) -> None:
        """Heuristic-fallback chain emits no intent — no bonus."""
        bonus = intent_alignment_bonus(
            memory_intent=None,
            aligned_intents=frozenset({"habitual"}),
        )
        assert bonus == 0.0

    def test_returns_zero_when_memory_intent_empty(self) -> None:
        """Empty string treated like missing label."""
        bonus = intent_alignment_bonus(
            memory_intent="",
            aligned_intents=frozenset({"habitual"}),
        )
        assert bonus == 0.0

    def test_returns_zero_when_aligned_set_empty(self) -> None:
        """QueryIntent with no alignment rule → no bonus."""
        bonus = intent_alignment_bonus(
            memory_intent="habitual",
            aligned_intents=frozenset(),
        )
        assert bonus == 0.0

    def test_returns_zero_when_aligned_set_none(self) -> None:
        """No mapping for the QueryIntent → no bonus."""
        bonus = intent_alignment_bonus(
            memory_intent="habitual",
            aligned_intents=None,
        )
        assert bonus == 0.0

    def test_accepts_set_or_tuple_aligned_collection(self) -> None:
        """Caller may pass set / frozenset / tuple — all work."""
        for collection in (
            {"habitual", "choice"},
            frozenset({"habitual"}),
            ("habitual", "choice"),
        ):
            bonus = intent_alignment_bonus(
                memory_intent="habitual",
                aligned_intents=collection,
                bonus=0.7,
            )
            assert bonus == 0.7

    def test_multiple_aligned_intents_any_match_wins(self) -> None:
        """STRATEGIC_REFLECTION aligns with habitual + choice."""
        bonus = intent_alignment_bonus(
            memory_intent="choice",
            aligned_intents=frozenset({"habitual", "choice"}),
        )
        assert bonus == 0.5

    def test_custom_bonus_value(self) -> None:
        bonus = intent_alignment_bonus(
            memory_intent="habitual",
            aligned_intents=frozenset({"habitual"}),
            bonus=1.25,
        )
        assert bonus == 1.25
