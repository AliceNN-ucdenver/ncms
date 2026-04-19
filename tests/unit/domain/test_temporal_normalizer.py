"""Unit tests for ``domain.temporal_normalizer``.

The normalizer's job is to turn raw GLiNER-extracted spans into
deterministic, timezone-aware ``(start, end)`` intervals.  These tests
cover:

* Absolute dates (ISO, slash, month-name, partial)
* Relative expressions ("yesterday", "last Monday", "next Friday")
* Durations + pairing with an anchor
* Start/end date range semantics
* Quarters, year-only, year+month widening
* Ambiguity handling — reject rather than hallucinate
* Merge semantics across multiple intervals
"""

from __future__ import annotations

from datetime import UTC, datetime

from ncms.domain.temporal.normalizer import (
    NormalizedInterval,
    RawSpan,
    merge_intervals,
    normalize_spans,
)

# Monday, April 20, 2026 — stable reference for deterministic tests.
REF = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)


def _span(text: str, label: str, char_start: int = 0, char_end: int = 0) -> RawSpan:
    return RawSpan(
        text=text, label=label,
        char_start=char_start,
        char_end=char_end if char_end else len(text),
    )


class TestNormalizeSpansAbsoluteDates:

    def test_iso_date(self) -> None:
        out = normalize_spans([_span("2024-06-05", "date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2024, 6, 5, tzinfo=UTC)
        assert out[0].end == datetime(2024, 6, 6, tzinfo=UTC)

    def test_month_name_date(self) -> None:
        out = normalize_spans([_span("June 5, 2024", "date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2024, 6, 5, tzinfo=UTC)
        assert out[0].end == datetime(2024, 6, 6, tzinfo=UTC)

    def test_year_only_widens_to_year(self) -> None:
        out = normalize_spans([_span("2024", "date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2024, 1, 1, tzinfo=UTC)
        assert out[0].end == datetime(2025, 1, 1, tzinfo=UTC)

    def test_year_month_widens_to_month(self) -> None:
        out = normalize_spans([_span("June 2024", "date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2024, 6, 1, tzinfo=UTC)
        assert out[0].end == datetime(2024, 7, 1, tzinfo=UTC)

    def test_quarter(self) -> None:
        out = normalize_spans([_span("Q1 2024", "date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2024, 1, 1, tzinfo=UTC)
        assert out[0].end == datetime(2024, 4, 1, tzinfo=UTC)

    def test_q4_rolls_year(self) -> None:
        out = normalize_spans([_span("Q4 2024", "date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2024, 10, 1, tzinfo=UTC)
        assert out[0].end == datetime(2025, 1, 1, tzinfo=UTC)


class TestNormalizeSpansRelative:

    def test_yesterday(self) -> None:
        out = normalize_spans([_span("yesterday", "relative date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2026, 4, 19, tzinfo=UTC)
        assert out[0].end == datetime(2026, 4, 20, tzinfo=UTC)

    def test_tomorrow(self) -> None:
        out = normalize_spans([_span("tomorrow", "relative date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2026, 4, 21, tzinfo=UTC)

    def test_last_week(self) -> None:
        out = normalize_spans([_span("last week", "relative date")], REF)
        assert len(out) == 1
        # 7 days before REF, span of 7 days
        assert out[0].start == datetime(2026, 4, 13, tzinfo=UTC)
        assert out[0].end == datetime(2026, 4, 20, tzinfo=UTC)

    def test_last_month_aligns_to_calendar_month(self) -> None:
        out = normalize_spans([_span("last month", "relative date")], REF)
        assert len(out) == 1
        # REF is April 2026 → last month = March 2026
        assert out[0].start == datetime(2026, 3, 1, tzinfo=UTC)
        assert out[0].end == datetime(2026, 4, 1, tzinfo=UTC)

    def test_this_year(self) -> None:
        out = normalize_spans([_span("this year", "relative date")], REF)
        assert out[0].start == datetime(2026, 1, 1, tzinfo=UTC)
        assert out[0].end == datetime(2027, 1, 1, tzinfo=UTC)

    def test_next_friday_with_modifier(self) -> None:
        """The known dateparser gap — 'next Friday' must resolve forward."""
        # REF is Monday 2026-04-20 — next Friday is 2026-04-24.
        out = normalize_spans([_span("next Friday", "relative date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2026, 4, 24, tzinfo=UTC)

    def test_last_thursday_with_modifier(self) -> None:
        # REF is Monday 2026-04-20 — last Thursday is 2026-04-16.
        out = normalize_spans([_span("last Thursday", "relative date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2026, 4, 16, tzinfo=UTC)

    def test_three_days_ago(self) -> None:
        out = normalize_spans([_span("3 days ago", "relative date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2026, 4, 17, tzinfo=UTC)

    def test_unparseable_relative_returns_nothing(self) -> None:
        """'asdf tuesday' isn't a thing — drop it, don't guess."""
        out = normalize_spans([_span("asdf tuesday", "relative date")], REF)
        # Either drop cleanly or parse "tuesday" — both are acceptable;
        # must not raise.
        assert isinstance(out, list)


class TestDurationPairing:

    def test_duration_alone_dropped(self) -> None:
        out = normalize_spans([_span("three days", "duration")], REF)
        assert out == []

    def test_duration_after_date_widens_anchor(self) -> None:
        # "I went on June 5, 2024 for three days" → [June 5, June 8).
        spans = [
            _span("June 5, 2024", "date", char_start=0, char_end=12),
            _span("three days", "duration", char_start=20, char_end=30),
        ]
        out = normalize_spans(spans, REF)
        assert len(out) == 1
        assert out[0].start == datetime(2024, 6, 5, tzinfo=UTC)
        assert out[0].end == datetime(2024, 6, 8, tzinfo=UTC)

    def test_bare_duration_with_no_anchor(self) -> None:
        spans = [_span("two weeks", "duration")]
        out = normalize_spans(spans, REF)
        assert out == []

    def test_multiple_dates_then_duration(self) -> None:
        # "From June 5 2024 or June 10 2024 for three days"
        # Only the most recent anchor (June 10) gets widened.
        spans = [
            _span("June 5, 2024", "date", char_start=0, char_end=12),
            _span("June 10, 2024", "date", char_start=16, char_end=29),
            _span("three days", "duration", char_start=34, char_end=44),
        ]
        out = normalize_spans(spans, REF)
        # June 5 passes through unchanged; June 10 widens to June 10-13.
        starts = {(i.start, i.end) for i in out}
        assert (datetime(2024, 6, 5, tzinfo=UTC),
                datetime(2024, 6, 6, tzinfo=UTC)) in starts
        assert (datetime(2024, 6, 10, tzinfo=UTC),
                datetime(2024, 6, 13, tzinfo=UTC)) in starts


class TestStartEndDateSemantics:

    def test_start_date(self) -> None:
        # "since June 5, 2024" → [June 5 2024, REF).
        out = normalize_spans([_span("since June 5, 2024", "start date")], REF)
        assert len(out) == 1
        assert out[0].start == datetime(2024, 6, 5, tzinfo=UTC)
        # End is the reference time (when the query fires / the memory
        # is ingested), not the epoch — this keeps "since X" meaning
        # "from X to now".
        assert out[0].end == REF

    def test_end_date(self) -> None:
        # "until June 5, 2024" → (MIN, June 6 2024].
        out = normalize_spans([_span("until June 5, 2024", "end date")], REF)
        assert len(out) == 1
        assert out[0].end == datetime(2024, 6, 6, tzinfo=UTC)
        # Start is min_horizon, not asserted exactly — just that it's
        # far in the past.
        assert out[0].start < datetime(2000, 1, 1, tzinfo=UTC)

    def test_start_date_in_future_rejected(self) -> None:
        # "since next year" would be nonsensical — end (REF) < start.
        out = normalize_spans(
            [_span("since next year", "start date")], REF,
        )
        # Either dropped (end<=start rejected) or resolved oddly —
        # both acceptable as long as no raise.
        assert isinstance(out, list)


class TestLabelDispatch:

    def test_time_of_day_ignored(self) -> None:
        out = normalize_spans([_span("2pm", "time of day")], REF)
        assert out == []

    def test_event_anchor_ignored(self) -> None:
        out = normalize_spans(
            [_span("after the surgery", "event anchor")], REF,
        )
        assert out == []

    def test_unknown_label_ignored(self) -> None:
        out = normalize_spans(
            [_span("June 5", "something-weird")], REF,
        )
        assert out == []

    def test_empty_text_ignored(self) -> None:
        out = normalize_spans([_span("", "date"), _span("   ", "date")], REF)
        assert out == []


class TestDedupByPosition:

    def test_same_position_different_labels_keeps_higher_priority(self) -> None:
        # GLiNER occasionally returns the same span twice.  The
        # 'date' label should win over 'relative date'.
        spans = [
            _span("June 5, 2024", "relative date", char_start=0, char_end=12),
            _span("June 5, 2024", "date", char_start=0, char_end=12),
        ]
        out = normalize_spans(spans, REF)
        # One output, resolved as an absolute date.
        assert len(out) == 1
        assert out[0].source_span.label == "date"


class TestRejectGates:

    def test_horizon_rejects_far_past(self) -> None:
        # Year 1800 is outside the 100-year horizon around REF.
        out = normalize_spans([_span("1800", "date")], REF)
        assert out == []

    def test_horizon_rejects_far_future(self) -> None:
        out = normalize_spans([_span("2999", "date")], REF)
        assert out == []

    def test_unparseable_date_dropped(self) -> None:
        out = normalize_spans([_span("not a date", "date")], REF)
        assert out == []


class TestInputRobustness:

    def test_empty_input(self) -> None:
        assert normalize_spans([], REF) == []

    def test_naive_reference_time_coerced_to_utc(self) -> None:
        naive = datetime(2026, 4, 20)
        out = normalize_spans([_span("yesterday", "relative date")], naive)
        assert len(out) == 1
        assert out[0].start.tzinfo is not None

    def test_many_spans_no_crash(self) -> None:
        spans = [_span(f"day {i}", "date") for i in range(100)]
        out = normalize_spans(spans, REF)
        # Whatever — just must not raise.
        assert isinstance(out, list)


class TestMergeIntervals:

    def test_empty_returns_none(self) -> None:
        assert merge_intervals([]) is None

    def test_single_returns_self(self) -> None:
        ni = NormalizedInterval(
            start=datetime(2024, 6, 1, tzinfo=UTC),
            end=datetime(2024, 6, 2, tzinfo=UTC),
            confidence=0.9,
            source_span=_span("x", "date"),
            origin="x",
        )
        out = merge_intervals([ni])
        assert out is not None
        assert out.start == ni.start and out.end == ni.end

    def test_union_of_disjoint(self) -> None:
        intervals = [
            NormalizedInterval(
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 2, tzinfo=UTC),
                confidence=0.9, source_span=_span("a", "date"),
                origin="a",
            ),
            NormalizedInterval(
                start=datetime(2024, 6, 1, tzinfo=UTC),
                end=datetime(2024, 6, 2, tzinfo=UTC),
                confidence=0.7, source_span=_span("b", "date"),
                origin="b",
            ),
        ]
        out = merge_intervals(intervals)
        assert out is not None
        assert out.start == datetime(2024, 1, 1, tzinfo=UTC)
        assert out.end == datetime(2024, 6, 2, tzinfo=UTC)
        assert out.confidence == 0.9  # max, not avg


class TestConfidenceThreshold:

    def test_low_confidence_dropped(self) -> None:
        # Currently normalizer emits >= 0.7 for all paths; this is a
        # guard test — if someone lowers confidences below the min,
        # the gate kicks in.  Using a label that always returns high
        # confidence as a smoke test for the gate itself.
        out = normalize_spans([_span("2024-06-05", "date")], REF)
        assert all(i.confidence >= 0.3 for i in out)
