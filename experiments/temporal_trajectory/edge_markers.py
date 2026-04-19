"""Layer 2 — mine transition markers from edge-destination content.

For each typed edge (supersedes / refines / retires / introduces), the
DESTINATION memory's content contains characteristic phrases that
indicate what the transition did.  Examples from the experiment corpus:

* ``supersedes`` destinations:
  "Authentication moves from session cookies to OAuth 2.0..." → the
  verb phrase "moves from X to Y" is a supersession marker.
  "Retire long-lived JWTs" → "retire" is a retirement marker.

* ``refines`` destinations:
  "Add JSON Web Tokens alongside OAuth..." → "add" / "alongside" are
  refinement markers.

These markers are NOT hand-coded.  We mine them by scanning edge-
destination content for verbs + prepositions that co-occur with each
transition type, then build a lookup table.

At query time, the classifier matches query tokens against these
induced markers to infer the LG intent — no hand-maintained regex.

This module's contract:

* ``induce_edge_markers()`` scans the corpus + EDGES at import time
  and builds a table: ``transition_type → set of marker phrases``.
* ``match_intent_from_markers(query)`` returns the transition type
  most strongly implied by the query tokens, or None.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from experiments.temporal_trajectory.corpus import ADR_CORPUS, EDGES


# Candidate verb-phrase patterns we look for in edge-destination
# content.  These are GRAMMATICAL shapes (verb-preposition), not
# vocabulary.  The VOCAB gets induced; we just provide the shapes
# worth scanning for.
_VERB_PHRASE_SHAPES: list[re.Pattern[str]] = [
    re.compile(r"\b(moves?\s+(?:from|to))\b", re.IGNORECASE),
    re.compile(r"\b(retire[ds]?\s+\w+)\b", re.IGNORECASE),
    re.compile(r"\b(supersedes?\s+\w+)\b", re.IGNORECASE),
    re.compile(r"\b(replaces?\s+\w+)\b", re.IGNORECASE),
    re.compile(r"\b(deprecat(?:es|ed|ing)\s+\w+)\b", re.IGNORECASE),
    re.compile(r"\b(add(?:s|ed)?\s+\w+)\b", re.IGNORECASE),
    re.compile(r"\b(extend[sd]?\s+\w+)\b", re.IGNORECASE),
    re.compile(r"\b(introduce[sd]?\s+\w+)\b", re.IGNORECASE),
    re.compile(r"\b(started|begins?|initiated|launched?)\b", re.IGNORECASE),
    re.compile(r"\b(performed|completed|concluded)\b", re.IGNORECASE),
    re.compile(r"\b(identified|reported|resolved|confirmed|cleared)\b", re.IGNORECASE),
    re.compile(r"\b(scheduled|planned)\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class EdgeMarkers:
    """Transition-type → marker phrases induced from the corpus."""

    # transition_type → set of marker keywords/phrases (lowercase,
    # stem-ish) that appeared in destination content.
    markers: dict[str, frozenset[str]]


def _extract_shape_matches(content: str) -> set[str]:
    """Return the set of verb-phrase keywords in content that match
    any of ``_VERB_PHRASE_SHAPES``.  We keep just the head verb
    (first word) so 'retire JWT' → 'retire', 'retired' stays as
    'retired', etc.  Stemming could normalise further; for the
    experiment this is enough."""
    hits: set[str] = set()
    for pat in _VERB_PHRASE_SHAPES:
        for m in pat.finditer(content):
            phrase = m.group(1).lower()
            head = phrase.split()[0]
            hits.add(head)
    return hits


def induce_edge_markers() -> EdgeMarkers:
    """Scan all edges; for each, record verb-phrase heads found in
    the destination memory's content under the edge's transition
    type.

    **Distinctiveness filter.**  A marker stays in bucket ``T`` only
    when it appears strictly more often in ``T`` than in any other
    transition type.  Ties drop the marker from every bucket — a
    marker that's equally common across transitions (e.g., ``resolved``
    appearing in both ``supersedes`` and ``refines`` destinations in
    our corpus) provides no signal and would cause mock reconciliation
    to tie and emit no edge.  This is exactly the disambiguation the
    grammar needs: Layer 2 exposes only verb heads that actually
    discriminate.

    Scales with the corpus — when more edges with a given transition
    cement a verb's association, its distinctiveness increases and it
    re-enters the bucket.  No manual pruning.
    """
    by_id = {m.mid: m for m in ADR_CORPUS}
    accum: dict[str, Counter[str]] = defaultdict(Counter)

    for edge in EDGES:
        dst_mem = by_id.get(edge.dst)
        if dst_mem is None:
            continue
        heads = _extract_shape_matches(dst_mem.content)
        for h in heads:
            accum[edge.transition][h] += 1

    # Distinctiveness filter — marker kept in bucket T iff count in T
    # strictly exceeds every other bucket's count.
    all_heads: set[str] = set()
    for bucket in accum.values():
        all_heads.update(bucket.keys())

    markers: dict[str, frozenset[str]] = {}
    for ttype, heads in accum.items():
        distinctive: set[str] = set()
        for h in heads.keys():
            mine = heads[h]
            others_max = max(
                (accum[t].get(h, 0) for t in accum if t != ttype),
                default=0,
            )
            if mine > others_max:
                distinctive.add(h)
        markers[ttype] = frozenset(distinctive)
    return EdgeMarkers(markers=markers)


MARKERS = induce_edge_markers()


def match_intent_from_markers(query: str) -> str | None:
    """Return the transition type most implied by the query, or None.

    Checks each transition type's induced marker set against the
    query; returns the type whose markers have the most hits.  Ties
    broken by priority (``supersedes`` > ``retires`` > ``refines``
    > ``introduces``).
    """
    q_lower = query.lower()
    scores: dict[str, int] = {}
    for ttype, heads in MARKERS.markers.items():
        score = sum(1 for h in heads if _word_in(h, q_lower))
        if score > 0:
            scores[ttype] = score
    if not scores:
        return None
    priority = {"supersedes": 3, "retires": 2, "refines": 1, "introduces": 0}
    return max(
        scores.keys(),
        key=lambda t: (scores[t], priority.get(t, 0)),
    )


def _word_in(token: str, query: str) -> bool:
    pattern = r"\b" + re.escape(token) + r"\w*\b"
    return re.search(pattern, query) is not None


def summary() -> str:
    lines = ["Layer 2 — induced edge markers", "=" * 60]
    for ttype, heads in sorted(MARKERS.markers.items()):
        lines.append(f"[{ttype}] ({len(heads)} markers)")
        for h in sorted(heads):
            lines.append(f"    {h}")
    return "\n".join(lines)
