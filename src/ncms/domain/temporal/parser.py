"""Temporal query parser — extract time references from search queries.

Pure domain logic with no infrastructure dependencies.
Regex-based parsing covers ~80% of temporal queries without LLM.

Phase 4 temporal integration: parsed references feed into the scoring
pipeline as an additive signal alongside BM25, SPLADE, Graph.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Word-to-number mapping for written numbers
_WORD_NUMBERS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
}

_MONTH_NAMES: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True, slots=True)
class TemporalReference:
    """Parsed temporal reference from a search query.

    Attributes:
        range_start: Start of the time window (inclusive).
        range_end: End of the time window (inclusive).
        recency_bias: True for "latest", "most recent", "current".
        ordinal: "first"/"earliest" or "last"/"latest" for ordering preference.
    """

    range_start: datetime | None = None
    range_end: datetime | None = None
    recency_bias: bool = False
    ordinal: str | None = None


def _parse_number(s: str) -> int | None:
    """Parse a number from digit string or word."""
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    return _WORD_NUMBERS.get(s)


def _month_range(year: int, month: int) -> tuple[datetime, datetime]:
    """Return (start, end) datetimes for a given year/month."""
    _, last_day = calendar.monthrange(year, month)
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=UTC)
    return start, end


def _quarter_range(year: int, quarter: int) -> tuple[datetime, datetime]:
    """Return (start, end) datetimes for a given year/quarter."""
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    _, last_day = calendar.monthrange(year, end_month)
    start = datetime(year, start_month, 1, tzinfo=UTC)
    end = datetime(year, end_month, last_day, 23, 59, 59, tzinfo=UTC)
    return start, end


# ---------------------------------------------------------------------------
# Pre-compiled pattern set — built once at import time.
#
# The dispatcher (``parse_temporal_reference``) walks each ``_match_*``
# handler in order and returns the first non-None result.  Order
# matters — more-specific patterns (named month + year) must run
# before more-general ones (bare year) so we don't shadow.
# ---------------------------------------------------------------------------

_NUM_ALT = r"(?:\d+|" + "|".join(_WORD_NUMBERS) + r")"
_MONTH_ALT = "|".join(_MONTH_NAMES)

_RE_RECENCY = re.compile(r"\b(latest|most\s+recent|current(?:ly)?|newest|up\s*to\s*date)\b")
_RE_FIRST = re.compile(r"\b(first|initial|earliest|original)\b")
_RE_YESTERDAY = re.compile(r"\byesterday\b")
_RE_TODAY = re.compile(r"\btoday\b")
_RE_DAYS_AGO = re.compile(rf"\b({_NUM_ALT})\s+days?\s+ago\b")
_RE_LAST_WEEK = re.compile(r"\blast\s+week\b")
_RE_WEEKS_AGO = re.compile(rf"\b({_NUM_ALT})\s+weeks?\s+ago\b")
_RE_LAST_MONTH = re.compile(r"\blast\s+month\b")
_RE_MONTHS_AGO = re.compile(rf"\b({_NUM_ALT})\s+months?\s+ago\b")
_RE_NAMED_MONTH = re.compile(rf"\b(?:in|since|during|from)\s+({_MONTH_ALT})(?:\s+(\d{{4}}))?\b")
_RE_BARE_YEAR = re.compile(r"\b(?:in|during|from|since)\s+(\d{4})\b")
_RE_QUARTER = re.compile(r"\bq([1-4])\s*(\d{4})?\b")
_RE_THIS_QUARTER = re.compile(r"\bthis\s+quarter\b")
_RE_LAST_QUARTER = re.compile(r"\blast\s+quarter\b")
_RE_LAST_N_DAYS = re.compile(rf"\b(?:last|past)\s+({_NUM_ALT})\s+days?\b")
_RE_SINCE = re.compile(r"\bsince\b")


# ---------------------------------------------------------------------------
# Per-pattern matchers.  Each one checks a single temporal shape and
# returns a :class:`TemporalReference` on hit, ``None`` on miss.
# ---------------------------------------------------------------------------


def _match_recency(q: str, now: datetime) -> TemporalReference | None:
    """Recency keywords — "latest", "most recent", "current"."""
    if _RE_RECENCY.search(q) is None:
        return None
    return TemporalReference(
        range_start=now - timedelta(hours=48),
        range_end=now,
        recency_bias=True,
        ordinal="last",
    )


def _match_first(q: str, now: datetime) -> TemporalReference | None:
    """Ordinal-first keywords — "first", "initial", "earliest", "original"."""
    if _RE_FIRST.search(q) is None:
        return None
    return TemporalReference(ordinal="first")


def _match_yesterday(q: str, now: datetime) -> TemporalReference | None:
    if _RE_YESTERDAY.search(q) is None:
        return None
    day_start = (now - timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    day_end = day_start.replace(hour=23, minute=59, second=59)
    return TemporalReference(range_start=day_start, range_end=day_end)


def _match_today(q: str, now: datetime) -> TemporalReference | None:
    if _RE_TODAY.search(q) is None:
        return None
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return TemporalReference(range_start=day_start, range_end=now)


def _match_days_ago(q: str, now: datetime) -> TemporalReference | None:
    m = _RE_DAYS_AGO.search(q)
    if m is None:
        return None
    n = _parse_number(m.group(1))
    if n is None:
        return None
    day_start = (now - timedelta(days=n)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    day_end = day_start.replace(hour=23, minute=59, second=59)
    return TemporalReference(range_start=day_start, range_end=day_end)


def _match_last_week(q: str, now: datetime) -> TemporalReference | None:
    """ "Last week" — the 7-day window ending on last Sunday."""
    if _RE_LAST_WEEK.search(q) is None:
        return None
    end = (now - timedelta(days=now.weekday() + 1)).replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=0,
    )
    start = (end - timedelta(days=6)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return TemporalReference(range_start=start, range_end=end)


def _match_weeks_ago(q: str, now: datetime) -> TemporalReference | None:
    m = _RE_WEEKS_AGO.search(q)
    if m is None:
        return None
    n = _parse_number(m.group(1))
    if n is None:
        return None
    center = now - timedelta(weeks=n)
    start = (center - timedelta(days=center.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end = (start + timedelta(days=6)).replace(hour=23, minute=59, second=59)
    return TemporalReference(range_start=start, range_end=end)


def _match_last_month(q: str, now: datetime) -> TemporalReference | None:
    if _RE_LAST_MONTH.search(q) is None:
        return None
    if now.month == 1:
        year, month = now.year - 1, 12
    else:
        year, month = now.year, now.month - 1
    start, end = _month_range(year, month)
    return TemporalReference(range_start=start, range_end=end)


def _match_months_ago(q: str, now: datetime) -> TemporalReference | None:
    m = _RE_MONTHS_AGO.search(q)
    if m is None:
        return None
    n = _parse_number(m.group(1))
    if n is None:
        return None
    year, month = now.year, now.month
    for _ in range(n):
        month -= 1
        if month < 1:
            month = 12
            year -= 1
    start, end = _month_range(year, month)
    return TemporalReference(range_start=start, range_end=end)


def _named_month_year(
    match: re.Match[str],
    now: datetime,
) -> int:
    """Resolve the year for a named-month match, preferring past.

    When the query has no explicit year and the named month is in
    the future of ``now``'s calendar year, we interpret it as last
    year ("in April" asked in March 2026 → April 2025).  The day-
    of-month fudge-factor (``now.day < 15``) handles the edge case
    where the month just rolled over.
    """
    if match.group(2):
        return int(match.group(2))
    month_num = _MONTH_NAMES[match.group(1)]
    rolled_over = month_num > now.month or (month_num == now.month and now.day < 15)
    return now.year - 1 if rolled_over else now.year


def _match_named_month(q: str, now: datetime) -> TemporalReference | None:
    """ "in March", "in January 2026", "since April"."""
    m = _RE_NAMED_MONTH.search(q)
    if m is None:
        return None
    month_num = _MONTH_NAMES[m.group(1)]
    year = _named_month_year(m, now)
    start, end = _month_range(year, month_num)
    if _RE_SINCE.search(q) is not None:
        end = now
    return TemporalReference(range_start=start, range_end=end)


def _match_bare_year(q: str, now: datetime) -> TemporalReference | None:
    """ "in 2024", "since 2024" — must run AFTER ``_match_named_month``
    so "in March 2024" doesn't get shadowed."""
    m = _RE_BARE_YEAR.search(q)
    if m is None:
        return None
    year = int(m.group(1))
    start = datetime(year, 1, 1, tzinfo=UTC)
    end = datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC)
    if _RE_SINCE.search(q) is not None:
        end = now
    return TemporalReference(range_start=start, range_end=end)


def _match_quarter(q: str, now: datetime) -> TemporalReference | None:
    """Named quarter: "Q1 2026"."""
    m = _RE_QUARTER.search(q)
    if m is None:
        return None
    quarter = int(m.group(1))
    year = int(m.group(2)) if m.group(2) else now.year
    start, end = _quarter_range(year, quarter)
    return TemporalReference(range_start=start, range_end=end)


def _match_this_quarter(q: str, now: datetime) -> TemporalReference | None:
    if _RE_THIS_QUARTER.search(q) is None:
        return None
    quarter = (now.month - 1) // 3 + 1
    start, end = _quarter_range(now.year, quarter)
    return TemporalReference(range_start=start, range_end=end)


def _match_last_quarter(q: str, now: datetime) -> TemporalReference | None:
    if _RE_LAST_QUARTER.search(q) is None:
        return None
    quarter = (now.month - 1) // 3 + 1
    if quarter == 1:
        quarter, year = 4, now.year - 1
    else:
        quarter, year = quarter - 1, now.year
    start, end = _quarter_range(year, quarter)
    return TemporalReference(range_start=start, range_end=end)


def _match_last_n_days(q: str, now: datetime) -> TemporalReference | None:
    """ "last 7 days" / "past 14 days"."""
    m = _RE_LAST_N_DAYS.search(q)
    if m is None:
        return None
    n = _parse_number(m.group(1))
    if n is None:
        return None
    start = (now - timedelta(days=n)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return TemporalReference(range_start=start, range_end=now)


# Ordered list of matchers.  The dispatcher returns the first non-None.
_MATCHERS: tuple = (
    _match_recency,
    _match_first,
    _match_yesterday,
    _match_today,
    _match_days_ago,
    _match_last_week,
    _match_weeks_ago,
    _match_last_month,
    _match_months_ago,
    _match_named_month,
    _match_bare_year,
    _match_quarter,
    _match_this_quarter,
    _match_last_quarter,
    _match_last_n_days,
)


def parse_temporal_reference(
    query: str,
    now: datetime | None = None,
) -> TemporalReference | None:
    """Extract a :class:`TemporalReference` from ``query``.

    Runs each pattern matcher in the documented order and returns the
    first hit; returns ``None`` when no matcher fires.  Patterns
    handled:

    - Relative days: "yesterday", "3 days ago", "last week", "2 weeks ago"
    - Relative months: "last month", "two months ago"
    - Named months: "in March", "in January 2026", "since April"
    - Bare years: "in 2024", "since 2023"
    - Named quarters: "Q1 2026", "this quarter", "last quarter"
    - Rolling windows: "last 7 days", "past 14 days"
    - Recency keywords: "latest", "most recent", "current", "newest"
    - Ordinal / earliest: "first", "initial", "earliest", "original"
    """
    if now is None:
        now = datetime.now(UTC)
    q = query.lower().strip()
    for matcher in _MATCHERS:
        result = matcher(q, now)
        if result is not None:
            return result
    return None


def compute_temporal_proximity(
    event_time: datetime,
    ref: TemporalReference,
    now: datetime | None = None,
) -> float:
    """Score [0, 1] how well event_time matches the temporal reference.

    Scoring strategy depends on the reference type:
    - recency_bias: exponential decay from now (recent = higher)
    - range: 1.0 inside the range, gaussian falloff outside
    - ordinal=="first": earlier timestamps score higher (inverse recency)

    Args:
        event_time: Timestamp of the memory/event to score.
        ref: Parsed temporal reference from the query.
        now: Current time (defaults to UTC now).

    Returns:
        Score in [0.0, 1.0]. Higher = better match.
    """
    if now is None:
        now = datetime.now(UTC)

    # Ensure timezone-aware comparison
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=UTC)

    if ref.ordinal == "first":
        # Earlier is better. Use inverse exponential decay.
        # Score 1.0 for very old events, decaying toward 0.0 for recent ones.
        # Half-life of 30 days: events 30 days old score ~0.5 relative to now.
        age_seconds = max(0.0, (now - event_time).total_seconds())
        age_days = age_seconds / 86400.0
        # Sigmoid-like: 1 - exp(-age/halflife)
        half_life = 30.0
        return 1.0 - math.exp(-math.log(2) * age_days / half_life)

    if ref.recency_bias:
        # Exponential decay from now. Half-life = 2 days.
        age_seconds = max(0.0, (now - event_time).total_seconds())
        age_days = age_seconds / 86400.0
        decay_rate = math.log(2) / 2.0  # 2-day half-life
        return math.exp(-decay_rate * age_days)

    if ref.range_start is not None or ref.range_end is not None:
        r_start = ref.range_start or datetime.min.replace(tzinfo=UTC)
        r_end = ref.range_end or now

        # Inside the range: perfect score
        if r_start <= event_time <= r_end:
            return 1.0

        # Outside the range: gaussian falloff
        # Sigma = range width (or 1 day minimum)
        range_seconds = max(
            (r_end - r_start).total_seconds(),
            86400.0,  # 1-day minimum sigma
        )

        if event_time < r_start:
            distance_seconds = (r_start - event_time).total_seconds()
        else:
            distance_seconds = (event_time - r_end).total_seconds()

        # Gaussian: exp(-0.5 * (distance / sigma)^2)
        ratio = distance_seconds / range_seconds
        return math.exp(-0.5 * ratio * ratio)

    # No range or recency — no temporal signal
    return 0.5
