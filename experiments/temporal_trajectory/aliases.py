"""Alias inference — derives entity aliases from the corpus.

The grammar's retirement/still/cause_of lookups match the query's
entity against the reconciler's ``retires_entities`` set.  Users
often query with a different surface form than what reconciliation
recorded:

  * Query: "Do we still use JSON Web Tokens?"  Reconciled retires: {JWT}
  * Query: "Is the patient in physical therapy?"  Retires: {PT}
  * Query: "Do we still do multi-factor authentication?"  Retires: {MFA}

Without alias inference the lookup misses and falls back to BM25.
This module derives aliases from one high-precision signal:

**Initials abbreviation** — ``short`` is an alias of ``full`` iff
``short``'s letters equal the initial letters of ``full``'s words
(case-insensitive, punctuation-stripped).  Both surface forms must
actually appear in the corpus (as memory entities or edge
``retires_entities``).

What this catches:

* JWT ↔ JSON Web Tokens
* MFA ↔ multi-factor authentication
* PT ↔ physical therapy
* NSAID ↔ (nonsteroidal anti-inflammatory drug) — if both surfaces
  appeared in corpus

What this does NOT catch (out of scope — would need semantic
similarity or a curated synonym table):

* delay ↔ blocker  (semantic aliases, no shared letters)
* stop ↔ halt       (lexical synonyms)

For those, the grammar's content-marker fallbacks (cause_of step (c))
already cover the common cases.  Integration note: NCMS's entity
graph could carry a richer alias table (from GLiNER labels,
spaCy synonym extraction, or LLM-assisted synonymy) without
changing this API.
"""

from __future__ import annotations

import re
from collections import defaultdict

from experiments.temporal_trajectory.corpus import ADR_CORPUS, EDGES


def _initials(phrase: str) -> str:
    """Lowercase first-letter-of-each-word.  Returns '' for single-word.

    Splits on whitespace *and* hyphens so "multi-factor authentication"
    produces initials "mfa" (three words) rather than "ma" (two)."""
    words = [w for w in re.split(r"[\s\-]+", phrase.strip()) if w]
    if len(words) < 2:
        return ""
    return "".join(w[0].lower() for w in words if w)


def _normalize(surface: str) -> str:
    return re.sub(r"[^\w]", "", surface.strip().lower())


def _is_abbreviation(short: str, full: str) -> bool:
    """``short`` is an abbreviation of ``full`` when its letters equal
    the initials of ``full``'s words."""
    short_norm = _normalize(short)
    full_initials = _initials(full)
    if not full_initials or len(short_norm) < 2:
        return False
    # Allow trailing 's' on short form (JWTs abbreviates "JSON Web Tokens").
    if short_norm.endswith("s"):
        if short_norm[:-1] == full_initials:
            return True
    return short_norm == full_initials


def induce_aliases() -> dict[str, frozenset[str]]:
    """Build canonical → alias-set table from corpus signals.

    **Optimization.**  Naive pairwise enumeration over all entities is
    O(|entities|²) — empirically ~30 s at 5000 memories.  Since
    abbreviations are always SHORT (2-8 chars, one word) and full
    forms are always LONG (multi-word phrases), we partition entities
    into short / long buckets and compare short × long instead of
    all × all.  This is O(|short| × |long|) — typically a ~30× speedup
    at 5000 memories and still O(n²) worst-case but with a much
    smaller constant.
    """
    all_entities: set[str] = set()
    for m in ADR_CORPUS:
        all_entities |= m.entities
    for e in EDGES:
        all_entities |= e.retires_entities

    # Partition: short candidates (potential abbreviations) vs
    # long candidates (multi-word full forms).  A short entity is
    # 2-8 chars, contains no whitespace-or-hyphen; a long entity
    # has at least 2 tokens (split on whitespace OR hyphen).
    shorts: list[str] = []
    longs: list[str] = []
    for ent in all_entities:
        n_tokens = len(re.split(r"[\s\-]+", ent.strip()))
        if n_tokens >= 2:
            longs.append(ent)
        normed = re.sub(r"[^\w]", "", ent.strip())
        if 2 <= len(normed) <= 8 and n_tokens == 1:
            shorts.append(ent)

    aliases: dict[str, set[str]] = defaultdict(set)
    for short in shorts:
        for full in longs:
            if _is_abbreviation(short, full):
                aliases[short].add(full)
                aliases[full].add(short)

    return {k: frozenset(v) for k, v in aliases.items()}


ALIASES: dict[str, frozenset[str]] = induce_aliases()


def expand_aliases(entity: str) -> frozenset[str]:
    """Return ``{entity}`` unioned with all known aliases.  Case-insensitive."""
    out: set[str] = {entity}
    ent_low = entity.lower()
    for canon, als in ALIASES.items():
        if canon.lower() == ent_low:
            out |= als
    return frozenset(out)


def summary() -> str:
    lines = ["Induced aliases", "=" * 40]
    if not ALIASES:
        lines.append("  (none)")
        return "\n".join(lines)
    seen: set[frozenset[str]] = set()
    for canon, als in sorted(ALIASES.items()):
        group = frozenset({canon, *als})
        if group in seen:
            continue
        seen.add(group)
        lines.append(f"  {' ↔ '.join(sorted(group))}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
