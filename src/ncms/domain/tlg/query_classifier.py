"""Minimal query-intent classifier (retired).

.. deprecated:: 2026-04
   Superseded by :mod:`ncms.domain.tlg.query_parser.analyze_query`
   which handles 12 intents with proper slot filling.  This module
   is retained only so older callers that imported
   ``classify_query_intent`` keep working during the transition; no
   production path in the TLG pipeline references it.  Safe to
   delete once downstream integrations migrate to
   ``analyze_query``.

Hand-coded regex patterns pick the intent from the three primitive
dispatch classes: ``current`` / ``origin`` / ``still``.  Subject and
entity slots are NOT filled here — callers had to do that
separately (which is exactly why the structural parser subsumed
this module).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# ``still`` — asks whether an entity is currently in use.  Matched
# before ``current`` because patterns like "do we still use X" also
# contain current-tense markers.
_STILL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bare\s+we\s+still\b", re.IGNORECASE),
    re.compile(r"\bdo\s+we\s+still\b", re.IGNORECASE),
    re.compile(r"\bis\s+\w+\s+still\b", re.IGNORECASE),
    re.compile(r"\bstill\s+(using|use|in\s+use)\b", re.IGNORECASE),
    re.compile(r"\bhave\s+we\s+retired\b", re.IGNORECASE),
    re.compile(r"\bdid\s+we\s+retire\b", re.IGNORECASE),
)

# ``origin`` — asks about the first / initial / original state.
_ORIGIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\boriginal\b", re.IGNORECASE),
    re.compile(r"\binitial(ly)?\b", re.IGNORECASE),
    re.compile(r"\bfirst(\s+ever)?\b", re.IGNORECASE),
    re.compile(r"\bstart(?:ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bbegin(?:s|ning)?\b", re.IGNORECASE),
    re.compile(r"\boriginate[sd]?\b", re.IGNORECASE),
)

# ``current`` — asks for the now-state of a subject.
_CURRENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcurrent(ly)?\b", re.IGNORECASE),
    re.compile(r"\blatest\b", re.IGNORECASE),
    re.compile(r"\bright\s+now\b", re.IGNORECASE),
    re.compile(r"\btoday\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(is|'s)\s+[\w\s]+?\s+using\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(do|does)\s+[\w\s]+?\s+use\b", re.IGNORECASE),
)


#: Intent names in evaluation order.  ``still`` runs first because
#: its patterns subsume some of the ``current`` patterns.
INTENT_ORDER: tuple[str, ...] = ("still", "origin", "current")


def _match_any(
    patterns: Iterable[re.Pattern[str]], query: str,
) -> bool:
    return any(p.search(query) is not None for p in patterns)


def classify_query_intent(query: str) -> str | None:
    """Return the intent name for ``query``, or ``None`` when no
    pattern matches.

    .. deprecated:: 2026-04
       Use :func:`ncms.domain.tlg.analyze_query` — it returns a
       populated :class:`QueryStructure` with subject + entity
       slots, not just the intent name.
    """
    import warnings
    warnings.warn(
        "classify_query_intent is deprecated; "
        "use ncms.domain.tlg.analyze_query instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    if not query:
        return None
    if _match_any(_STILL_PATTERNS, query):
        return "still"
    if _match_any(_ORIGIN_PATTERNS, query):
        return "origin"
    if _match_any(_CURRENT_PATTERNS, query):
        return "current"
    return None
