"""Minimal query-intent classifier for Phase 3c dispatch.

Hand-coded regex patterns pick the intent from the three primitive
dispatch classes implemented so far: ``current`` / ``origin`` /
``still``.  Subject and entity slots are filled by the caller from
the L1 :class:`InducedVocabulary` (usually via
:class:`ncms.application.tlg.VocabularyCache`).

Phase 4+ intents (``sequence`` / ``predecessor`` / ``interval`` /
``range``) are not classified here — they require the query parser
in ``experiments/temporal_trajectory/query_parser.py`` which is
~900 lines and pulls in more machinery (temporal normaliser, alias
expansion).  Port those when the corresponding dispatch paths land.

The patterns are the tiny hand-maintained "grammar atom" lexicon
referenced in ``docs/temporal-linguistic-geometry.md`` §6.  They
encode English interrogative structure, not domain vocabulary; the
vocabulary grows by induction (:mod:`.vocabulary`,
:mod:`.markers`).
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

    Cheap and deterministic — pure regex, no LLM, no I/O.  Intended
    to be called once per query at dispatch time.
    """
    if not query:
        return None
    if _match_any(_STILL_PATTERNS, query):
        return "still"
    if _match_any(_ORIGIN_PATTERNS, query):
        return "origin"
    if _match_any(_CURRENT_PATTERNS, query):
        return "current"
    return None
