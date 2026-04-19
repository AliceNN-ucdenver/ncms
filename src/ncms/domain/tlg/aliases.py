"""Alias induction — derives entity aliases from the corpus.

Port of ``experiments/temporal_trajectory/aliases.py`` adapted for
NCMS: the corpus is passed as an iterable of surface-form strings
instead of reading globals.  Application-layer code composes the
input from the MemoryStore's entity registry + graph edges'
``retires_entities``; this module stays stateless.

The grammar's retirement / still / cause_of lookups match the
query's entity against the reconciler's ``retires_entities`` set.
Users often query with a different surface form than what
reconciliation recorded:

* Query: "Do we still use JSON Web Tokens?"  Retires: {JWT}
* Query: "Is the patient in physical therapy?"  Retires: {PT}
* Query: "Do we still do multi-factor authentication?"  Retires: {MFA}

Without alias expansion the lookup misses and falls back to BM25.
This module mines aliases from one high-precision signal:

**Initials abbreviation** — ``short`` is an alias of ``full`` iff
``short``'s letters equal the initial letters of ``full``'s words
(case-insensitive, punctuation-stripped, hyphen-aware so
"multi-factor authentication" → "MFA").  Both surface forms must
actually appear in the corpus.

Out of scope (would require semantic similarity / curated tables):

* delay ↔ blocker    (semantic)
* stop ↔ halt         (lexical synonym)

See ``docs/temporal-linguistic-geometry.md`` §6.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable


def _initials(phrase: str) -> str:
    """First-letter-of-each-word, lowercase.  Empty for single-word.

    Splits on whitespace *and* hyphens so
    ``"multi-factor authentication"`` → ``"mfa"`` (three words)
    rather than ``"ma"`` (two).
    """
    words = [w for w in re.split(r"[\s\-]+", phrase.strip()) if w]
    if len(words) < 2:
        return ""
    return "".join(w[0].lower() for w in words if w)


def _normalize(surface: str) -> str:
    """Strip punctuation and lowercase — for abbreviation comparison."""
    return re.sub(r"[^\w]", "", surface.strip().lower())


def _is_abbreviation(short: str, full: str) -> bool:
    """True iff ``short``'s letters match the initials of ``full``.

    Tolerates a trailing ``s`` on the abbreviation — so ``"JWTs"``
    abbreviates ``"JSON Web Tokens"``.
    """
    short_norm = _normalize(short)
    full_initials = _initials(full)
    if not full_initials or len(short_norm) < 2:
        return False
    if short_norm.endswith("s") and short_norm[:-1] == full_initials:
        return True
    return short_norm == full_initials


def _partition(
    entities: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Split entities into short (abbreviation-candidate) and long
    (full-form) buckets.

    * **short**: 2-8 chars after punctuation-strip, single token
    * **long**: ≥ 2 tokens (split on whitespace or hyphen)

    The partition is what takes alias induction from O(n²) to
    O(|short| × |long|) — empirically ~30× speedup at 5 k memories.
    """
    shorts: list[str] = []
    longs: list[str] = []
    for ent in entities:
        stripped = ent.strip()
        if not stripped:
            continue
        tokens = re.split(r"[\s\-]+", stripped)
        if len(tokens) >= 2:
            longs.append(ent)
        normed = re.sub(r"[^\w]", "", stripped)
        if 2 <= len(normed) <= 8 and len(tokens) == 1:
            shorts.append(ent)
    return shorts, longs


def induce_aliases(
    entities: Iterable[str],
) -> dict[str, frozenset[str]]:
    """Build a canonical-surface → alias-set mapping from the input
    entities.

    Mapping is bidirectional — ``aliases["JWT"] = {"JSON Web Tokens"}``
    and ``aliases["JSON Web Tokens"] = {"JWT"}``.  Case is preserved
    on the keys so callers can round-trip back to the original
    surface form; lookups should be case-insensitive.

    Returns an empty dict when the input has no abbreviation
    candidates — safe to call on cold corpora.
    """
    shorts, longs = _partition(entities)
    aliases: dict[str, set[str]] = defaultdict(set)
    for short in shorts:
        for full in longs:
            if _is_abbreviation(short, full):
                aliases[short].add(full)
                aliases[full].add(short)
    return {k: frozenset(v) for k, v in aliases.items()}


def expand_aliases(
    entity: str,
    aliases: dict[str, frozenset[str]],
) -> frozenset[str]:
    """Return ``{entity}`` unioned with every known alias of ``entity``.

    Case-insensitive lookup over ``aliases`` keys — callers can pass
    any surface form.  Result always contains the original ``entity``
    so callers can apply the same downstream matching rules whether
    or not any aliases existed.
    """
    out: set[str] = {entity}
    needle = entity.lower()
    for canon, als in aliases.items():
        if canon.lower() == needle:
            out |= als
    return frozenset(out)


def summary(aliases: dict[str, frozenset[str]]) -> str:
    """Human-readable dump of the bidirectional alias groups."""
    lines = ["Induced aliases", "=" * 40]
    if not aliases:
        lines.append("  (none)")
        return "\n".join(lines)
    seen: set[frozenset[str]] = set()
    for canon, als in sorted(aliases.items()):
        group = frozenset({canon, *als})
        if group in seen:
            continue
        seen.add(group)
        lines.append(f"  {' <-> '.join(sorted(group))}")
    return "\n".join(lines)
