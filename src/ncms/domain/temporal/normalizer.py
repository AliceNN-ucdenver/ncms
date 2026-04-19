"""Temporal span → ISO interval normalizer (domain layer, pure).

Post-processing for GLiNER-extracted temporal spans.  Turns raw text
labeled by GLiNER (``date``, ``relative date``, ``duration`` ...) into
deterministic, timezone-aware intervals suitable for use as a
retrieval range filter.

Why a dedicated module:

* ``dateparser`` parses many expressions but returns a single point
  datetime, not an interval.
* ``dateparser`` alone doesn't handle "next/last + weekday", partial
  dates that should widen to their natural container ("June 2024"
  = the month of June, not June 18), duration pairing with a date
  anchor, quarters, or the range semantics of ``start date``/``end
  date`` labels.
* The rules must be unit-testable in isolation.

See ``docs/p1-temporal-experiment.md`` §3 for the design rationale.

Zero infrastructure dependencies — this module imports only stdlib
and ``dateparser``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import dateparser

TemporalLabel = Literal[
    "date",
    "relative date",
    "duration",
    "start date",
    "end date",
    "time of day",
    "event anchor",
]

# Canonical priority — higher entries win when the same span is
# emitted with multiple labels by GLiNER.
_LABEL_PRIORITY: dict[str, int] = {
    "date": 6,
    "relative date": 5,
    "duration": 4,
    "start date": 3,
    "end date": 2,
    "time of day": 1,
    "event anchor": 0,
}

# Horizon beyond which a parse is rejected as implausible (prevents
# "12" from being interpreted as year 0012 etc.).
_MAX_HORIZON_YEARS = 100

# Minimum confidence for a resolved interval to be retained.
_MIN_CONFIDENCE = 0.3

# Leading modifier → direction bias for weekday/month parsing.
_MODIFIER_RE = re.compile(
    r"^(next|upcoming|coming|this|last|previous|past)\s+(.+)$",
    re.IGNORECASE,
)
_FUTURE_MODIFIERS = {"next", "upcoming", "coming"}
_PAST_MODIFIERS = {"last", "previous", "past"}

# Bare duration pattern: "3 days", "a couple of weeks", "six months".
_DURATION_RE = re.compile(
    r"^\s*(a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"a\s+couple\s+of|a\s+few|several|\d+)\s+"
    r"(second|minute|hour|day|week|month|quarter|year)s?\s*$",
    re.IGNORECASE,
)
_NUMBER_WORDS: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a couple of": 2, "a few": 3, "several": 3,
}

# Quarter pattern: "Q1 2024", "Q4/2023"
_QUARTER_RE = re.compile(
    r"^\s*q([1-4])\s*[/\- ]\s*(\d{4})\s*$",
    re.IGNORECASE,
)

# Partial-date patterns
_YEAR_ONLY_RE = re.compile(r"^\s*\d{4}\s*$")
_YEAR_MONTH_RE = re.compile(
    r"^\s*(january|february|march|april|may|june|july|august|"
    r"september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s*[,/\- ]\s*\d{4}\s*$",
    re.IGNORECASE,
)

# Common phrases that dateparser struggles with — map to
# (offset_days_from_ref, span_days).
_CANONICAL_PHRASES: dict[str, tuple[int, int]] = {
    # text (lowercased) → (start_offset_days, span_days)
    "today": (0, 1),
    "tonight": (0, 1),
    "yesterday": (-1, 1),
    "tomorrow": (1, 1),
    "this week": (0, 7),             # approx — refined in resolve
    "last week": (-7, 7),
    "next week": (7, 7),
    "this month": (0, 30),           # refined to calendar month
    "last month": (-30, 30),
    "next month": (30, 30),
    "this year": (0, 365),
    "last year": (-365, 365),
    "next year": (365, 365),
    "this weekend": (0, 2),          # approx
    "last weekend": (-7, 2),
    "next weekend": (7, 2),
    "upcoming weekend": (7, 2),
}


@dataclass(frozen=True)
class RawSpan:
    """A span extracted by GLiNER before normalization."""

    text: str
    label: str
    char_start: int = 0
    char_end: int = 0


@dataclass(frozen=True)
class NormalizedInterval:
    """A resolved (start, end) interval with provenance."""

    start: datetime
    end: datetime       # exclusive upper bound
    confidence: float
    source_span: RawSpan
    # Extra context for duration pairing / merging.
    origin: str = ""    # "date" | "relative" | "duration+anchor" | ...


@dataclass
class _ResolutionContext:
    """Internal state carried through per-call normalization."""

    reference_time: datetime
    max_horizon: datetime = field(init=False)
    min_horizon: datetime = field(init=False)

    def __post_init__(self) -> None:
        years = timedelta(days=365 * _MAX_HORIZON_YEARS)
        self.max_horizon = self.reference_time + years
        self.min_horizon = self.reference_time - years


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_spans(
    spans: list[RawSpan],
    reference_time: datetime,
) -> list[NormalizedInterval]:
    """Deterministic span → interval mapping.

    Spans that can't be resolved (unparseable, ambiguous, outside the
    horizon, below confidence threshold) are dropped — the returned
    list may be shorter than the input.  Never raises on bad input.

    ``reference_time`` must be timezone-aware; naive inputs are
    coerced to UTC.  All returned intervals are timezone-aware (UTC).
    """
    if not spans:
        return []
    ref = _ensure_utc(reference_time)
    ctx = _ResolutionContext(reference_time=ref)

    # Step 1: deduplicate by (char_start, char_end), keeping highest-priority label.
    deduped = _dedupe_by_position(spans)

    # Step 2: resolve each span in isolation, then pair durations with
    # adjacent dates.
    initial: list[tuple[RawSpan, NormalizedInterval | None]] = [
        (s, _resolve_single(s, ctx)) for s in deduped
    ]
    paired = _pair_durations_with_anchors(initial, ctx)

    # Step 3: apply reject gates.
    out: list[NormalizedInterval] = []
    for ni in paired:
        if ni is None:
            continue
        if ni.confidence < _MIN_CONFIDENCE:
            continue
        if ni.start < ctx.min_horizon or ni.end > ctx.max_horizon:
            continue
        if ni.end <= ni.start:  # degenerate
            continue
        out.append(ni)
    return out


def merge_intervals(
    intervals: list[NormalizedInterval],
) -> NormalizedInterval | None:
    """Reduce a list of intervals to a single union interval.

    Returns ``None`` for an empty input.  Confidence is the max of
    inputs (we surface the strongest signal, not the average).
    """
    if not intervals:
        return None
    start = min(i.start for i in intervals)
    end = max(i.end for i in intervals)
    best = max(intervals, key=lambda i: i.confidence)
    return NormalizedInterval(
        start=start, end=end,
        confidence=best.confidence,
        source_span=best.source_span,
        origin="merged",
    )


# ---------------------------------------------------------------------------
# Internal resolution helpers
# ---------------------------------------------------------------------------


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _dedupe_by_position(spans: list[RawSpan]) -> list[RawSpan]:
    """Keep the highest-priority label per (char_start, char_end)."""
    best_by_key: dict[tuple[int, int], RawSpan] = {}
    unposed: list[RawSpan] = []
    for s in spans:
        key = (s.char_start, s.char_end)
        if key == (0, 0) and s not in unposed:
            # No positions — keep the whole span as-is, dedup by text+label
            if all(
                not (u.text == s.text and u.label == s.label)
                for u in unposed
            ):
                unposed.append(s)
            continue
        prev = best_by_key.get(key)
        if prev is None or _priority(s) > _priority(prev):
            best_by_key[key] = s
    return unposed + list(best_by_key.values())


def _priority(span: RawSpan) -> int:
    return _LABEL_PRIORITY.get(span.label, -1)


def _resolve_single(
    span: RawSpan, ctx: _ResolutionContext,
) -> NormalizedInterval | None:
    """Resolve one span, dispatching by label."""
    text = (span.text or "").strip()
    if not text:
        return None
    label = span.label

    if label == "date":
        return _resolve_date(span, text, ctx)
    if label == "relative date":
        return _resolve_relative(span, text, ctx)
    if label == "duration":
        # Bare duration has no interval on its own — returned as a
        # marker so _pair_durations_with_anchors can see it.
        return _resolve_duration_marker(span, text)
    if label == "start date":
        return _resolve_start_date(span, text, ctx)
    if label == "end date":
        return _resolve_end_date(span, text, ctx)
    if label == "time of day":
        return None  # not used for the range filter
    if label == "event anchor":
        return None  # not resolvable without event graph
    return None


def _resolve_date(
    span: RawSpan, text: str, ctx: _ResolutionContext,
) -> NormalizedInterval | None:
    """Absolute dates: widen partials to the natural container."""
    # Year-only → [YYYY-01-01, YYYY+1-01-01)
    if _YEAR_ONLY_RE.match(text):
        try:
            year = int(text.strip())
            start = datetime(year, 1, 1, tzinfo=UTC)
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
            return NormalizedInterval(
                start=start, end=end, confidence=0.95,
                source_span=span, origin="date:year",
            )
        except ValueError:
            return None
    # Year+month → [YYYY-MM-01, next month)
    if _YEAR_MONTH_RE.match(text):
        parsed = _dp_parse(text, ctx)
        if parsed is None:
            return None
        start = datetime(parsed.year, parsed.month, 1, tzinfo=UTC)
        end = _add_month(start)
        return NormalizedInterval(
            start=start, end=end, confidence=0.92,
            source_span=span, origin="date:year-month",
        )
    # Quarter → [Q-start, next-Q start)
    qm = _QUARTER_RE.match(text)
    if qm:
        q = int(qm.group(1))
        year = int(qm.group(2))
        q_month = (q - 1) * 3 + 1  # 1, 4, 7, 10
        start = datetime(year, q_month, 1, tzinfo=UTC)
        end = datetime(year + (1 if q == 4 else 0),
                       (q_month + 3) if q != 4 else 1, 1, tzinfo=UTC)
        return NormalizedInterval(
            start=start, end=end, confidence=0.9,
            source_span=span, origin="date:quarter",
        )
    # Fully-specified date.
    parsed = _dp_parse(text, ctx)
    if parsed is None:
        return None
    start = _to_utc_midnight(parsed)
    end = start + timedelta(days=1)
    return NormalizedInterval(
        start=start, end=end, confidence=0.88,
        source_span=span, origin="date:absolute",
    )


def _resolve_relative(
    span: RawSpan, text: str, ctx: _ResolutionContext,
) -> NormalizedInterval | None:
    """Relative expressions: canonical phrases first, then dateparser."""
    text_lc = text.lower().strip()

    # Canonical phrase table — always beats dateparser for consistency.
    if text_lc in _CANONICAL_PHRASES:
        offset_days, span_days = _CANONICAL_PHRASES[text_lc]
        anchor = _to_utc_midnight(ctx.reference_time)
        start = anchor + timedelta(days=offset_days)
        # Calendar-aware refinements for week/month/year:
        if text_lc == "this month":
            start = datetime(anchor.year, anchor.month, 1, tzinfo=UTC)
            end = _add_month(start)
            return _mk(span, start, end, 0.9, "relative:this-month")
        if text_lc == "last month":
            this_m = datetime(anchor.year, anchor.month, 1, tzinfo=UTC)
            start = _subtract_month(this_m)
            end = this_m
            return _mk(span, start, end, 0.9, "relative:last-month")
        if text_lc == "next month":
            this_m = datetime(anchor.year, anchor.month, 1, tzinfo=UTC)
            start = _add_month(this_m)
            end = _add_month(start)
            return _mk(span, start, end, 0.9, "relative:next-month")
        if text_lc == "this year":
            start = datetime(anchor.year, 1, 1, tzinfo=UTC)
            end = datetime(anchor.year + 1, 1, 1, tzinfo=UTC)
            return _mk(span, start, end, 0.9, "relative:this-year")
        if text_lc == "last year":
            start = datetime(anchor.year - 1, 1, 1, tzinfo=UTC)
            end = datetime(anchor.year, 1, 1, tzinfo=UTC)
            return _mk(span, start, end, 0.9, "relative:last-year")
        if text_lc == "next year":
            start = datetime(anchor.year + 1, 1, 1, tzinfo=UTC)
            end = datetime(anchor.year + 2, 1, 1, tzinfo=UTC)
            return _mk(span, start, end, 0.9, "relative:next-year")
        end = start + timedelta(days=span_days)
        return _mk(span, start, end, 0.9, "relative:canonical")

    # Leading modifier: "next Friday", "last Thursday".
    mod_match = _MODIFIER_RE.match(text_lc)
    if mod_match:
        modifier = mod_match.group(1).lower()
        rest = mod_match.group(2).strip()
        direction = None
        if modifier in _FUTURE_MODIFIERS:
            direction = "future"
        elif modifier in _PAST_MODIFIERS:
            direction = "past"
        parsed = _dp_parse(rest, ctx, direction=direction)
        if parsed is not None:
            start = _to_utc_midnight(parsed)
            end = start + timedelta(days=1)
            return _mk(span, start, end, 0.85, "relative:modifier")

    # Fallback: dateparser on the whole string.
    parsed = _dp_parse(text, ctx)
    if parsed is None:
        return None
    start = _to_utc_midnight(parsed)
    end = start + timedelta(days=1)
    return _mk(span, start, end, 0.7, "relative:fallback")


def _resolve_duration_marker(
    span: RawSpan, text: str,
) -> NormalizedInterval | None:
    """Parse a bare duration into a sentinel interval.

    Return a zero-length sentinel with start==end so the pairing step
    can identify it.  We tag origin='duration' and let
    _pair_durations_with_anchors consume it.
    """
    days = _parse_duration_to_days(text)
    if days is None:
        return None
    sentinel = datetime(1, 1, 1, tzinfo=UTC)
    # Carry the duration in confidence's fractional part is a terrible
    # hack — instead, encode it by storing the delta as start=sentinel
    # and end=sentinel+delta.  The pairing step uses (end - start) as
    # the duration.
    return NormalizedInterval(
        start=sentinel,
        end=sentinel + timedelta(days=days),
        confidence=0.75,
        source_span=span,
        origin="duration",
    )


def _resolve_start_date(
    span: RawSpan, text: str, ctx: _ResolutionContext,
) -> NormalizedInterval | None:
    """'since June 5' → [June 5, reference_time)."""
    clean = re.sub(
        r"^(since|starting|from|as\s+of|after)\s+", "", text, flags=re.IGNORECASE,
    ).strip()
    parsed = _dp_parse(clean, ctx)
    if parsed is None:
        return None
    start = _to_utc_midnight(parsed)
    end = ctx.reference_time
    if end <= start:
        return None
    return _mk(span, start, end, 0.82, "start-date")


def _resolve_end_date(
    span: RawSpan, text: str, ctx: _ResolutionContext,
) -> NormalizedInterval | None:
    """'until yesterday' → [MIN, yesterday+1)."""
    clean = re.sub(
        r"^(until|through|by|before|to)\s+", "", text, flags=re.IGNORECASE,
    ).strip()
    parsed = _dp_parse(clean, ctx)
    if parsed is None:
        return None
    end = _to_utc_midnight(parsed) + timedelta(days=1)
    # No sensible absolute floor — use min_horizon as a permissive lower bound.
    start = ctx.min_horizon
    if end <= start:
        return None
    return _mk(span, start, end, 0.8, "end-date")


def _pair_durations_with_anchors(
    resolved: list[tuple[RawSpan, NormalizedInterval | None]],
    ctx: _ResolutionContext,
) -> list[NormalizedInterval | None]:
    """Pair `duration` markers with adjacent date anchors.

    Walk the resolved list once.  For each duration marker, try to
    extend the closest resolved date/relative-date into an interval
    of the marker's width.  Emit the extended interval and drop the
    bare duration marker.  Anchors not adjacent to a duration pass
    through unchanged.
    """
    out: list[NormalizedInterval | None] = []
    last_anchor_idx: int | None = None
    for idx, (_span, ni) in enumerate(resolved):
        if ni is None:
            out.append(None)
            continue
        if ni.origin == "duration":
            delta = ni.end - ni.start
            # Look left for a recent anchor we can widen.
            if (
                last_anchor_idx is not None
                and last_anchor_idx < len(out)
                and out[last_anchor_idx] is not None
            ):
                anchor = out[last_anchor_idx]
                assert anchor is not None
                widened = NormalizedInterval(
                    start=anchor.start,
                    end=anchor.start + delta,
                    confidence=min(anchor.confidence, ni.confidence),
                    source_span=anchor.source_span,
                    origin=f"{anchor.origin}+duration",
                )
                out[last_anchor_idx] = widened
                out.append(None)  # consume the duration
                continue
            # No anchor available — drop the bare duration.
            out.append(None)
            continue
        # Anchor-like: remember as the latest anchor.
        out.append(ni)
        last_anchor_idx = idx
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dp_parse(
    text: str, ctx: _ResolutionContext, direction: str | None = None,
) -> datetime | None:
    """Single entry point to dateparser with NCMS-standard settings."""
    settings: dict[str, object] = {
        "RELATIVE_BASE": ctx.reference_time.replace(tzinfo=None),
        "RETURN_AS_TIMEZONE_AWARE": False,
    }
    if direction:
        settings["PREFER_DATES_FROM"] = direction
    try:
        return dateparser.parse(text, settings=settings)
    except Exception:
        return None


def _to_utc_midnight(dt: datetime) -> datetime:
    """Coerce to UTC midnight (strip time-of-day)."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return datetime(dt.year, dt.month, dt.day, tzinfo=UTC)


def _add_month(dt: datetime) -> datetime:
    """dt is expected to be the 1st of a month; return 1st of next month."""
    year, month = dt.year, dt.month + 1
    if month > 12:
        year += 1
        month = 1
    return datetime(year, month, 1, tzinfo=UTC)


def _subtract_month(dt: datetime) -> datetime:
    year, month = dt.year, dt.month - 1
    if month < 1:
        year -= 1
        month = 12
    return datetime(year, month, 1, tzinfo=UTC)


def _parse_duration_to_days(text: str) -> int | None:
    """Return duration in days, or None on failure.

    Approximations: 1 month = 30 days, 1 quarter = 91 days,
    1 year = 365 days.  Enough for retrieval filtering.
    """
    m = _DURATION_RE.match(text.strip())
    if not m:
        return None
    number_s = m.group(1).lower().strip()
    unit = m.group(2).lower()
    if number_s.isdigit():
        n = int(number_s)
    else:
        n = _NUMBER_WORDS.get(number_s)
        if n is None:
            return None
    unit_to_days = {
        "second": 0,   # degenerate — rejected downstream by end<=start
        "minute": 0,
        "hour": 0,
        "day": 1,
        "week": 7,
        "month": 30,
        "quarter": 91,
        "year": 365,
    }
    d = unit_to_days.get(unit, 0)
    total = n * d
    if total <= 0:
        return None
    return total


def _mk(
    span: RawSpan,
    start: datetime,
    end: datetime,
    confidence: float,
    origin: str,
) -> NormalizedInterval:
    return NormalizedInterval(
        start=start, end=end, confidence=confidence,
        source_span=span, origin=origin,
    )
