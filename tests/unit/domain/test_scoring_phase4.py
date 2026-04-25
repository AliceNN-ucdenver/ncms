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
    role_grounding_bonus,
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


class TestRoleGroundingBonus:
    """Tests for role_grounding_bonus() — Phase H.3.

    Reward memories where the query's entity appears as ``role=primary``
    in the SLM's per-span output.  Conservative-by-design: only the
    PRIMARY role earns a bonus; casual / alternative / not_relevant
    return 0.0 in this version.  (Penalising those is a separate
    decision, deferred to a future phase if needed.)
    """

    def test_primary_match_returns_bonus(self) -> None:
        spans = [
            {
                "canonical": "postgresql", "role": "primary",
                "surface": "Postgres", "slot": "database",
            },
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"postgresql"},
        )
        assert bonus == 0.5

    def test_case_insensitive_match(self) -> None:
        """Canonicals compared lowercased on both sides."""
        spans = [
            {"canonical": "PostgreSQL", "role": "primary"},
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"postgresql"},
        )
        assert bonus == 0.5

    def test_casual_role_returns_zero(self) -> None:
        """Memory mentions the entity casually — no boost."""
        spans = [
            {"canonical": "redis", "role": "casual"},
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"redis"},
        )
        assert bonus == 0.0

    def test_alternative_role_returns_zero(self) -> None:
        """``role=alternative`` doesn't earn the primary bonus."""
        spans = [
            {"canonical": "mysql", "role": "alternative"},
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"mysql"},
        )
        assert bonus == 0.0

    def test_not_relevant_role_returns_zero(self) -> None:
        spans = [
            {"canonical": "kafka", "role": "not_relevant"},
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"kafka"},
        )
        assert bonus == 0.0

    def test_no_canonical_overlap_returns_zero(self) -> None:
        """Primary span exists but for a different entity."""
        spans = [
            {"canonical": "postgresql", "role": "primary"},
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"redis"},
        )
        assert bonus == 0.0

    def test_empty_role_spans_returns_zero(self) -> None:
        """Heuristic-fallback memories have empty role_spans."""
        assert role_grounding_bonus(
            role_spans=[],
            query_canonicals={"postgresql"},
        ) == 0.0
        assert role_grounding_bonus(
            role_spans=None,
            query_canonicals={"postgresql"},
        ) == 0.0

    def test_empty_query_canonicals_returns_zero(self) -> None:
        """Queries with no extracted entities get no grounding signal."""
        spans = [
            {"canonical": "postgresql", "role": "primary"},
        ]
        assert role_grounding_bonus(
            role_spans=spans,
            query_canonicals=set(),
        ) == 0.0
        assert role_grounding_bonus(
            role_spans=spans,
            query_canonicals=None,
        ) == 0.0

    def test_one_primary_among_many_wins(self) -> None:
        """A single primary match is enough; other spans don't matter."""
        spans = [
            {"canonical": "redis", "role": "casual"},
            {"canonical": "kafka", "role": "not_relevant"},
            {"canonical": "postgresql", "role": "primary"},
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"postgresql"},
        )
        assert bonus == 0.5

    def test_all_matching_spans_non_primary_returns_zero(self) -> None:
        """Multiple matches but none primary → no bonus."""
        spans = [
            {"canonical": "postgresql", "role": "casual"},
            {"canonical": "postgresql", "role": "alternative"},
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"postgresql"},
        )
        assert bonus == 0.0

    def test_malformed_span_skipped_not_raised(self) -> None:
        """Defensive: bad span shapes don't crash the scoring loop."""
        spans = [
            "not a dict",  # garbage
            {"role": "primary"},  # missing canonical
            {"canonical": None, "role": "primary"},  # null canonical
            {"canonical": "postgresql", "role": "primary"},  # good
        ]
        bonus = role_grounding_bonus(
            role_spans=spans,  # type: ignore[arg-type]
            query_canonicals={"postgresql"},
        )
        assert bonus == 0.5

    def test_custom_bonus_value(self) -> None:
        spans = [{"canonical": "postgresql", "role": "primary"}]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={"postgresql"},
            primary_bonus=1.0,
        )
        assert bonus == 1.0

    def test_empty_string_canonicals_filtered(self) -> None:
        """Empty/whitespace canonicals shouldn't accidentally match."""
        spans = [{"canonical": "", "role": "primary"}]
        bonus = role_grounding_bonus(
            role_spans=spans,
            query_canonicals={""},
        )
        assert bonus == 0.0
