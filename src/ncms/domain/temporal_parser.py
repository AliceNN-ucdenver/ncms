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
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30,
}

_MONTH_NAMES: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12,
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


def parse_temporal_reference(
    query: str,
    now: datetime | None = None,
) -> TemporalReference | None:
    """Extract temporal reference from a query string.

    Returns None if no temporal signal is detected.

    Pattern types handled:
    - Relative days: "yesterday", "3 days ago", "last week", "2 weeks ago"
    - Relative months: "last month", "two months ago", "3 months ago"
    - Named months: "in March", "in January 2026", "since April"
    - Named quarters: "Q1 2026", "this quarter", "last quarter"
    - Recency keywords: "latest", "most recent", "current", "newest"
    - Ordinal/earliest: "first", "initial", "earliest", "original"
    """
    if now is None:
        now = datetime.now(UTC)

    q = query.lower().strip()

    # ── Recency keywords ─────────────────────────────────────────────
    recency_pat = r"\b(latest|most\s+recent|current(?:ly)?|newest|up\s*to\s*date)\b"
    if re.search(recency_pat, q):
        return TemporalReference(
            range_start=now - timedelta(hours=48),
            range_end=now,
            recency_bias=True,
            ordinal="last",
        )

    # ── Ordinal: first/earliest ──────────────────────────────────────
    first_pat = r"\b(first|initial|earliest|original)\b"
    if re.search(first_pat, q):
        return TemporalReference(ordinal="first")

    # ── "yesterday" ──────────────────────────────────────────────────
    if re.search(r"\byesterday\b", q):
        day_start = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        day_end = day_start.replace(hour=23, minute=59, second=59)
        return TemporalReference(range_start=day_start, range_end=day_end)

    # ── "today" ──────────────────────────────────────────────────────
    if re.search(r"\btoday\b", q):
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = now
        return TemporalReference(range_start=day_start, range_end=day_end)

    # ── "N days ago" / "N day ago" ───────────────────────────────────
    m = re.search(r"\b(\d+|" + "|".join(_WORD_NUMBERS) + r")\s+days?\s+ago\b", q)
    if m:
        n = _parse_number(m.group(1))
        if n is not None:
            day_start = (now - timedelta(days=n)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            day_end = day_start.replace(hour=23, minute=59, second=59)
            return TemporalReference(range_start=day_start, range_end=day_end)

    # ── "last week" / "N weeks ago" ──────────────────────────────────
    if re.search(r"\blast\s+week\b", q):
        # Last week = 7-14 days ago
        end = (now - timedelta(days=now.weekday() + 1)).replace(
            hour=23, minute=59, second=59, microsecond=0,
        )
        start = (end - timedelta(days=6)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return TemporalReference(range_start=start, range_end=end)

    m = re.search(
        r"\b(\d+|" + "|".join(_WORD_NUMBERS) + r")\s+weeks?\s+ago\b", q,
    )
    if m:
        n = _parse_number(m.group(1))
        if n is not None:
            center = now - timedelta(weeks=n)
            start = (center - timedelta(days=center.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            end = (start + timedelta(days=6)).replace(
                hour=23, minute=59, second=59,
            )
            return TemporalReference(range_start=start, range_end=end)

    # ── "last month" / "N months ago" ────────────────────────────────
    if re.search(r"\blast\s+month\b", q):
        if now.month == 1:
            year, month = now.year - 1, 12
        else:
            year, month = now.year, now.month - 1
        start, end = _month_range(year, month)
        return TemporalReference(range_start=start, range_end=end)

    m = re.search(
        r"\b(\d+|" + "|".join(_WORD_NUMBERS) + r")\s+months?\s+ago\b", q,
    )
    if m:
        n = _parse_number(m.group(1))
        if n is not None:
            # Walk back N months
            year, month = now.year, now.month
            for _ in range(n):
                month -= 1
                if month < 1:
                    month = 12
                    year -= 1
            start, end = _month_range(year, month)
            return TemporalReference(range_start=start, range_end=end)

    # ── Named month: "in March", "in January 2026", "since April" ────
    month_names_pat = "|".join(_MONTH_NAMES)
    m = re.search(
        r"\b(?:in|since|during|from)\s+(" + month_names_pat + r")"
        r"(?:\s+(\d{4}))?\b",
        q,
    )
    if m:
        month_num = _MONTH_NAMES[m.group(1)]
        year = int(m.group(2)) if m.group(2) else now.year
        # If the named month is in the future this year, assume last year
        if not m.group(2) and (
            month_num > now.month
            or (month_num == now.month and now.day < 15)
        ):
            year -= 1
        start, end = _month_range(year, month_num)
        # "since April" means from April start to now
        if re.search(r"\bsince\b", q):
            end = now
        return TemporalReference(range_start=start, range_end=end)

    # ── Named quarter: "Q1 2026", "this quarter", "last quarter" ─────
    m = re.search(r"\bq([1-4])\s*(\d{4})?\b", q)
    if m:
        quarter = int(m.group(1))
        year = int(m.group(2)) if m.group(2) else now.year
        start, end = _quarter_range(year, quarter)
        return TemporalReference(range_start=start, range_end=end)

    if re.search(r"\bthis\s+quarter\b", q):
        quarter = (now.month - 1) // 3 + 1
        start, end = _quarter_range(now.year, quarter)
        return TemporalReference(range_start=start, range_end=end)

    if re.search(r"\blast\s+quarter\b", q):
        quarter = (now.month - 1) // 3 + 1
        if quarter == 1:
            quarter, year = 4, now.year - 1
        else:
            quarter, year = quarter - 1, now.year
        start, end = _quarter_range(year, quarter)
        return TemporalReference(range_start=start, range_end=end)

    # ── "last N days" / "past N days" ────────────────────────────────
    m = re.search(
        r"\b(?:last|past)\s+(\d+|" + "|".join(_WORD_NUMBERS) + r")\s+days?\b",
        q,
    )
    if m:
        n = _parse_number(m.group(1))
        if n is not None:
            start = (now - timedelta(days=n)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            return TemporalReference(range_start=start, range_end=now)

    # No temporal signal detected
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
