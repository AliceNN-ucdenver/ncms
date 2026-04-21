"""Temporal-intent classification — pure, zero-infrastructure.

.. deprecated:: 2026-04
   Superseded by the TLG structural query parser in
   :mod:`ncms.domain.tlg.query_parser`, which subsumes these
   primitive families with richer slot filling (``sequence`` /
   ``predecessor`` / ``interval`` / ``range`` intents).  Kept for
   the baseline ``temporal_range_filter_enabled=true, temporal_enabled
   =false`` path; slated for removal after TLG benchmark parity.

Classifies a user query into one of the six LLM-free retrieval routes
defined in ``docs/retired/p1-temporal-experiment.md`` §17.2.  The three
primitive families are:

* **Ordinal** — "first X" / "last X" / "which came first, A or B" /
  "in what order did X, Y, Z happen".  Retrieve entity-scoped memories,
  sort by ``observed_at``.
* **Range / Anchor** — "since June", "last week", "between X and Y
  dates".  Normalize to an ISO interval, hard-filter candidates.
* **Arithmetic** — "how many days between X and Y" / "how long since".
  Answered by `MemoryService.compute_temporal_arithmetic` (Phase B.5),
  retrieval skips its filtering work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal


class TemporalIntent(Enum):
    """Temporal-reasoning shape of a query."""

    NONE = "none"                     # no temporal signal → default retrieval
    ORDINAL_SINGLE = "ordinal_single"  # "first/last X" — one subject
    ORDINAL_COMPARE = "ordinal_compare"  # "which came first, A or B"
    ORDINAL_ORDER = "ordinal_order"    # "in what order did X, Y, Z happen"
    RANGE = "range"                   # "during X", "in June", "last week"
    RELATIVE_ANCHOR = "relative_anchor"  # "N units ago", "since X"
    ARITHMETIC = "arithmetic"          # "how many days between"


# Token patterns that indicate arithmetic over dates.  Classifier
# fast-fails to ARITHMETIC on these — retrieval can't score the answer
# string ("7 days"), so no filter is meaningful.
_ARITHMETIC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bhow\s+many\s+(?:days?|weeks?|months?|years?|hours?)",
               re.IGNORECASE),
    re.compile(r"\bhow\s+long\s+(?:ago|since|before|after|has|had)",
               re.IGNORECASE),
    re.compile(r"\b(?:days?|weeks?|months?|years?)\s+(?:between|since)",
               re.IGNORECASE),
    re.compile(r"\bduration\s+(?:between|of)", re.IGNORECASE),
]

# Tokens that hint at ordinal-compare vs ordinal-order structure.
_COMPARE_MARKERS = re.compile(
    r"\b(?:which|or|either)\b", re.IGNORECASE,
)
_ORDER_MARKERS = re.compile(
    r"\b(?:order\s+of|order\s+from|in\s+what\s+order|chronolog)",
    re.IGNORECASE,
)

# Explicit range tokens — the temporal parser will produce a range_start
# or range_end when these match its own patterns, but the classifier
# also notices them to disambiguate RANGE vs RELATIVE_ANCHOR upstream
# of the parser's output.
_RANGE_MARKERS = re.compile(
    r"\b(?:during|in\s+(?:january|february|march|april|may|june|"
    r"july|august|september|october|november|december|\d{4})|"
    r"between\s+\w+\s+and)\b",
    re.IGNORECASE,
)
_RELATIVE_MARKERS = re.compile(
    r"\b(?:ago|since|last\s+(?:week|month|year)|"
    r"this\s+(?:week|month|year)|earlier\s+today|earlier)\b",
    re.IGNORECASE,
)


def classify_temporal_intent(
    query: str,
    *,
    ordinal: str | None,
    has_range: bool,
    has_relative: bool,
    subject_count: int,
) -> TemporalIntent:
    """Decide the temporal-retrieval route for a query.

    Inputs come from upstream primitives that have already run:

    * ``ordinal`` — "first" / "last" / None (from ``temporal_parser``).
    * ``has_range`` — the parser emitted a concrete range (start/end).
    * ``has_relative`` — the parser emitted a relative expression
      (recency_bias, "N units ago", etc.).
    * ``subject_count`` — number of subject entities GLiNER extracted
      from the query.

    Precedence order (most specific first):

    1. **Arithmetic** — question is asking for a *duration*.  Regex-
       tested on the query text.  Short-circuits all other paths.
    2. **Ordinal-order** — 3+ subject entities AND ordinal intent, OR
       explicit order markers ("in what order did X, Y, Z happen").
    3. **Ordinal-compare** — 2 subject entities AND ordinal intent AND
       compare markers ("which came first, A or B").
    4. **Ordinal-single** — 1 subject entity AND ordinal intent.
    5. **Range** — has_range is True, OR query matches a range marker.
    6. **Relative-anchor** — has_relative is True, OR query matches a
       relative marker.
    7. **None** — none of the above.  Default retrieval.
    """
    q = query or ""

    # 1. Arithmetic fast-path.
    for pattern in _ARITHMETIC_PATTERNS:
        if pattern.search(q):
            return TemporalIntent.ARITHMETIC

    # 2-4. Ordinal family.
    if ordinal in ("first", "last"):
        if subject_count >= 3 or _ORDER_MARKERS.search(q):
            return TemporalIntent.ORDINAL_ORDER
        if subject_count == 2 or _COMPARE_MARKERS.search(q):
            return TemporalIntent.ORDINAL_COMPARE
        if subject_count == 1:
            return TemporalIntent.ORDINAL_SINGLE
        # Ordinal-worded but no subjects extracted → fall through.

    # 5. Range.
    if has_range or _RANGE_MARKERS.search(q):
        return TemporalIntent.RANGE

    # 6. Relative anchor.
    if has_relative or _RELATIVE_MARKERS.search(q):
        return TemporalIntent.RELATIVE_ANCHOR

    return TemporalIntent.NONE


# ── Phase B.5: Arithmetic sub-specification ─────────────────────────

ArithmeticOp = Literal["between", "since", "age_of"]
ArithmeticUnit = Literal["days", "weeks", "months", "years", "hours"]


@dataclass(frozen=True)
class ArithmeticSpec:
    """Sub-classification for an ARITHMETIC-intent query.

    Read by ``MemoryService.compute_temporal_arithmetic`` to pick the
    right retrieval + math path:

    * ``between`` — "how many X between A and B" — needs 2 anchors.
    * ``since`` — "how long since A" / "how many X since A" — needs
      1 anchor; the other side is the caller-supplied ``reference_time``.
    * ``age_of`` — "how long ago did A happen" — same as ``since``.

    ``unit`` drives the final delta rounding.
    """

    operation: ArithmeticOp
    unit: ArithmeticUnit


# How many anchors each operation needs.
ARITHMETIC_ANCHOR_COUNTS: dict[str, int] = {
    "between": 2,
    "since": 1,
    "age_of": 1,
}

# Unit detection — ordered longest-first so "months" doesn't lose to "month".
_UNIT_PATTERNS: list[tuple[re.Pattern[str], ArithmeticUnit]] = [
    (re.compile(r"\bhours?\b", re.IGNORECASE),  "hours"),
    (re.compile(r"\bdays?\b", re.IGNORECASE),   "days"),
    (re.compile(r"\bweeks?\b", re.IGNORECASE),  "weeks"),
    (re.compile(r"\bmonths?\b", re.IGNORECASE), "months"),
    (re.compile(r"\byears?\b", re.IGNORECASE),  "years"),
]

# Operation detection — regexes ordered by specificity.
_BETWEEN_RE = re.compile(
    r"\bbetween\b", re.IGNORECASE,
)
_SINCE_RE = re.compile(
    r"\b(?:since|after|from)\b", re.IGNORECASE,
)
_AGE_OF_RE = re.compile(
    r"\bhow\s+long\s+ago\b|\bhow\s+many\s+\w+\s+ago\b",
    re.IGNORECASE,
)


def parse_arithmetic_spec(query: str) -> ArithmeticSpec | None:
    """Sub-classify an arithmetic temporal query.

    Returns ``None`` when the query doesn't match any arithmetic
    pattern, or when the unit can't be determined.  The caller
    (``MemoryService.compute_temporal_arithmetic``) uses the
    returned spec to pick an anchor-resolution strategy and to
    format the result.

    Heuristic order:
      1. ``between`` marker → ``between`` op.
      2. ``ago`` marker (with "how long"/"how many") → ``age_of``.
      3. ``since`` / ``after`` / ``from`` marker → ``since``.

    Unit defaults to ``days`` when no specific unit is mentioned,
    on the assumption that a caller asking a duration question
    without a unit expects the most granular honest unit.
    """
    q = query or ""
    # Must be an arithmetic question at the intent level.  Short-
    # circuit if not — keeps the spec parser's contract narrow.
    is_arithmetic = any(p.search(q) for p in _ARITHMETIC_PATTERNS)
    if not is_arithmetic:
        return None

    # Operation.
    if _BETWEEN_RE.search(q):
        op: ArithmeticOp = "between"
    elif _AGE_OF_RE.search(q):
        op = "age_of"
    elif _SINCE_RE.search(q):
        op = "since"
    else:
        # "how many days I X"-style without a connective marker; treat
        # as age_of (anchored on reference_time).
        op = "age_of"

    # Unit.
    unit: ArithmeticUnit = "days"
    for pattern, candidate in _UNIT_PATTERNS:
        if pattern.search(q):
            unit = candidate
            break

    return ArithmeticSpec(operation=op, unit=unit)
