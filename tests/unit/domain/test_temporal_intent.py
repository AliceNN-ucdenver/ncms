"""Unit tests for ``classify_temporal_intent``.

One test per decision branch plus adversarial cases for precedence.
Pure function — easy to cover exhaustively.
"""

from __future__ import annotations

from ncms.domain.temporal.intent import (
    ArithmeticSpec,
    TemporalIntent,
    classify_temporal_intent,
    parse_arithmetic_spec,
)


def _classify(q: str, **overrides) -> TemporalIntent:
    """Compact helper with sensible defaults."""
    defaults = dict(
        ordinal=None,
        has_range=False,
        has_relative=False,
        subject_count=0,
    )
    defaults.update(overrides)
    return classify_temporal_intent(q, **defaults)


class TestArithmeticFastPath:
    def test_how_many_days_between(self) -> None:
        assert (
            _classify(
                "How many days between X and Y?",
                ordinal="first",
                subject_count=2,
            )
            == TemporalIntent.ARITHMETIC
        )

    def test_how_many_weeks(self) -> None:
        assert (
            _classify(
                "How many weeks ago did I visit my aunt?",
                subject_count=1,
                has_relative=True,
            )
            == TemporalIntent.ARITHMETIC
        )

    def test_how_long_since(self) -> None:
        assert (
            _classify(
                "How long has it been since the meeting?",
                subject_count=1,
                has_relative=True,
            )
            == TemporalIntent.ARITHMETIC
        )

    def test_duration_between(self) -> None:
        assert (
            _classify(
                "What is the duration between the two events?",
                subject_count=2,
            )
            == TemporalIntent.ARITHMETIC
        )


class TestOrdinalFamily:
    def test_ordinal_single_one_subject(self) -> None:
        assert (
            _classify(
                "What was the first ADR on authentication?",
                ordinal="first",
                subject_count=1,
            )
            == TemporalIntent.ORDINAL_SINGLE
        )

    def test_ordinal_compare_two_subjects(self) -> None:
        assert (
            _classify(
                "Which happened first, the MoMA visit or the Met exhibit?",
                ordinal="first",
                subject_count=2,
            )
            == TemporalIntent.ORDINAL_COMPARE
        )

    def test_ordinal_compare_markers_override_count(self) -> None:
        # Single subject but compare markers → still compare
        assert (
            _classify(
                "Which came first, A or B?",
                ordinal="first",
                subject_count=1,
            )
            == TemporalIntent.ORDINAL_COMPARE
        )

    def test_ordinal_order_three_subjects(self) -> None:
        assert (
            _classify(
                "Which three events happened first to last?",
                ordinal="first",
                subject_count=3,
            )
            == TemporalIntent.ORDINAL_ORDER
        )

    def test_ordinal_order_marker(self) -> None:
        # Two subjects but explicit order-of marker → order
        assert (
            _classify(
                "In what order did A, B, and C happen?",
                ordinal="first",
                subject_count=2,
            )
            == TemporalIntent.ORDINAL_ORDER
        )

    def test_ordinal_last_single(self) -> None:
        assert (
            _classify(
                "What was the last update to the spec?",
                ordinal="last",
                subject_count=1,
            )
            == TemporalIntent.ORDINAL_SINGLE
        )

    def test_ordinal_with_zero_subjects_falls_through(self) -> None:
        """Ordinal word present but no subjects extracted → not ordinal."""
        result = _classify(
            "What was the first thing?",
            ordinal="first",
            subject_count=0,
        )
        assert result != TemporalIntent.ORDINAL_SINGLE
        assert result != TemporalIntent.ORDINAL_COMPARE
        assert result != TemporalIntent.ORDINAL_ORDER


class TestRangeAndRelative:
    def test_range_from_parser(self) -> None:
        assert (
            _classify(
                "What happened in June 2024?",
                has_range=True,
            )
            == TemporalIntent.RANGE
        )

    def test_range_from_marker(self) -> None:
        assert (
            _classify(
                "What happened during the Q1 planning cycle?",
            )
            == TemporalIntent.RANGE
        )

    def test_relative_from_parser(self) -> None:
        assert (
            _classify(
                "What happened last month?",
                has_relative=True,
            )
            == TemporalIntent.RELATIVE_ANCHOR
        )

    def test_relative_from_marker(self) -> None:
        assert (
            _classify(
                "What did I say earlier?",
            )
            == TemporalIntent.RELATIVE_ANCHOR
        )


class TestPrecedence:
    def test_arithmetic_beats_ordinal(self) -> None:
        # Question has "first" but is really asking for a duration.
        assert (
            _classify(
                "How many weeks between my first and second visit?",
                ordinal="first",
                subject_count=2,
            )
            == TemporalIntent.ARITHMETIC
        )

    def test_ordinal_beats_range(self) -> None:
        assert (
            _classify(
                "What was the first event during June?",
                ordinal="first",
                subject_count=1,
                has_range=True,
            )
            == TemporalIntent.ORDINAL_SINGLE
        )

    def test_range_beats_relative(self) -> None:
        # Both signals present; range wins.
        assert (
            _classify(
                "What happened in June last year?",
                has_range=True,
                has_relative=True,
            )
            == TemporalIntent.RANGE
        )


class TestNoneDefault:
    def test_empty_query(self) -> None:
        assert _classify("") == TemporalIntent.NONE

    def test_no_temporal_signal(self) -> None:
        assert (
            _classify(
                "What is the capital of France?",
                subject_count=2,
            )
            == TemporalIntent.NONE
        )

    def test_ordinal_absent_subjects_only(self) -> None:
        """Subjects with no ordinal signal → NONE (named-entity case,
        handled by default retrieval, not temporal primitive)."""
        assert (
            _classify(
                "Tell me about MoMA and the Met",
                subject_count=2,
            )
            == TemporalIntent.NONE
        )


# ── Phase B.5 spec parser ────────────────────────────────────────────


class TestParseArithmeticSpec:
    def test_between_days(self) -> None:
        spec = parse_arithmetic_spec(
            "How many days between my MoMA visit and the Met exhibit?",
        )
        assert spec == ArithmeticSpec(operation="between", unit="days")

    def test_between_weeks(self) -> None:
        spec = parse_arithmetic_spec(
            "How many weeks between the two meetings?",
        )
        assert spec == ArithmeticSpec(operation="between", unit="weeks")

    def test_between_months(self) -> None:
        spec = parse_arithmetic_spec(
            "How many months have passed between A and B?",
        )
        assert spec == ArithmeticSpec(operation="between", unit="months")

    def test_since_weeks(self) -> None:
        spec = parse_arithmetic_spec(
            "How many weeks since I met my aunt?",
        )
        assert spec == ArithmeticSpec(operation="since", unit="weeks")

    def test_how_long_since(self) -> None:
        """'How long since X' has no unit → defaults to days."""
        spec = parse_arithmetic_spec("How long since the last deployment?")
        assert spec == ArithmeticSpec(operation="since", unit="days")

    def test_age_of_event(self) -> None:
        spec = parse_arithmetic_spec(
            "How long ago did I visit my aunt?",
        )
        assert spec == ArithmeticSpec(operation="age_of", unit="days")

    def test_how_many_weeks_ago(self) -> None:
        spec = parse_arithmetic_spec(
            "How many weeks ago did the release happen?",
        )
        assert spec == ArithmeticSpec(operation="age_of", unit="weeks")

    def test_non_arithmetic_query_returns_none(self) -> None:
        assert parse_arithmetic_spec("What is the capital of France?") is None

    def test_ordinal_query_returns_none(self) -> None:
        assert parse_arithmetic_spec("What was the first ADR?") is None

    def test_range_query_returns_none(self) -> None:
        assert parse_arithmetic_spec("What happened in 2024?") is None

    def test_empty_query(self) -> None:
        assert parse_arithmetic_spec("") is None

    def test_hours_unit(self) -> None:
        spec = parse_arithmetic_spec(
            "How many hours between the two alerts?",
        )
        assert spec == ArithmeticSpec(operation="between", unit="hours")
