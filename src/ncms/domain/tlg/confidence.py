"""Four-level confidence model — the composition invariant.

Only ``HIGH`` and ``MEDIUM`` grammar answers are prepended to the
BM25 ranking.  ``LOW``, ``ABSTAIN``, and ``NONE`` fall back to BM25
unchanged.  This is the core safety property:
``grammar ∨ BM25`` guarantees zero confidently-wrong answers
(Proposition 1 in ``docs/temporal-linguistic-geometry.md``,
empirically 0 / 500 on LongMemEval).

Levels:

* ``HIGH`` — grammar path is deterministic and slots resolved exactly
  (zone terminal found, edge lookup hit, alias match in
  ``retires_entities``).  Safe to trust at rank 1.
* ``MEDIUM`` — grammar path is well-defined but uses a minor
  approximation (entity-in-current-zone heuristic, content-marker
  fallback).  Good rank-1 candidate; BM25 ordering preserved below.
* ``LOW`` — grammar path used a loose fallback (generic
  entity-mention).  Answer is a hint; BM25 rank-1 typically still
  right.  NOT prepended.
* ``ABSTAIN`` — intent matched but slots could not be resolved.
  Grammar answer is ``None``; BM25 fallback unchanged.
* ``NONE`` — no intent matched; grammar didn't apply at all.
"""

from __future__ import annotations

from enum import StrEnum


class Confidence(StrEnum):
    """Ordered confidence levels for a grammar dispatch result."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    ABSTAIN = "abstain"
    NONE = "none"


#: The set of confidence levels that justify prepending the grammar
#: answer onto the BM25 ranking.  Membership in this set is the
#: composition-invariant predicate referenced by Proposition 1.
CONFIDENT_LEVELS: frozenset[Confidence] = frozenset(
    {Confidence.HIGH, Confidence.MEDIUM}
)


def is_confident(level: Confidence | str | None) -> bool:
    """Predicate — ``True`` iff the level justifies prepending."""
    if level is None:
        return False
    try:
        enum_level = Confidence(level) if isinstance(level, str) else level
    except ValueError:
        return False
    return enum_level in CONFIDENT_LEVELS
