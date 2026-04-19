"""L2 — Transition-marker induction (pure).

Port of ``experiments/temporal_trajectory/edge_markers.py`` adapted
for NCMS: the edges-with-destination-content are passed as an argument
instead of reading global ``EDGES`` / ``ADR_CORPUS``.  Application-layer
code (``application/tlg/induction``) is responsible for composing the
input from the MemoryStore and persisting the output into
``grammar_transition_markers`` (schema v12) — this module stays pure.

The induction approach from the research code is preserved intact:

* The GRAMMATICAL shapes (verb + optional preposition) are hand-coded
  — see :data:`VERB_PHRASE_SHAPES`.  English grammar is a fixed
  invariant; it's the VOCABULARY within those shapes that grows with
  the corpus.
* For each transition type (``supersedes`` / ``refines`` / ...), we
  scan destination-memory content for verb heads and count them.
* **Distinctiveness filter** — a verb head stays in bucket T iff its
  count in T strictly exceeds its count in every other bucket.  Ties
  drop the marker from every bucket, because an indistinctive marker
  provides no signal for the downstream dispatch classifier.

See ``docs/temporal-linguistic-geometry.md`` §5 and
``docs/p1-plan.md`` Appendix F.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Fixed verb-phrase shapes (grammar, not vocabulary)
# ---------------------------------------------------------------------------

VERB_PHRASE_SHAPES: list[re.Pattern[str]] = [
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


# ---------------------------------------------------------------------------
# Inputs + outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeObservation:
    """A single typed edge paired with its destination-memory content.

    Caller (``application/tlg/induction``) assembles these by
    fetching :class:`~ncms.domain.models.GraphEdge` rows for the
    relevant transition types and resolving ``target_id`` to the
    underlying Memory's content.
    """

    transition: str
    dst_content: str


@dataclass(frozen=True)
class InducedEdgeMarkers:
    """Transition-type → set of distinctive verb-phrase heads."""

    markers: dict[str, frozenset[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_verb_heads(content: str) -> set[str]:
    """Return the set of verb-phrase heads in ``content`` matching
    :data:`VERB_PHRASE_SHAPES`.

    "head" is the first word of the matched phrase lowercased, so
    ``"retire JWT"`` → ``"retire"``, ``"moves from"`` → ``"moves"``,
    ``"deprecates X"`` → ``"deprecates"``.
    """
    heads: set[str] = set()
    for pat in VERB_PHRASE_SHAPES:
        for m in pat.finditer(content):
            phrase = m.group(1).lower()
            heads.add(phrase.split()[0])
    return heads


# ---------------------------------------------------------------------------
# Induction
# ---------------------------------------------------------------------------


def induce_edge_markers(
    observations: Iterable[EdgeObservation],
) -> InducedEdgeMarkers:
    """Mine distinctive verb-phrase markers per transition type.

    For each observation, extracts verb heads from ``dst_content`` and
    attributes them to the observation's transition.  After the full
    pass, applies a distinctiveness filter: a head is retained in
    bucket T iff its count in T strictly exceeds its count in every
    other bucket.

    Pure: no I/O, no mutable state outside the function.
    """
    accum: dict[str, Counter[str]] = defaultdict(Counter)
    for obs in observations:
        if not obs.transition or not obs.dst_content:
            continue
        for head in extract_verb_heads(obs.dst_content):
            accum[obs.transition][head] += 1

    markers: dict[str, frozenset[str]] = {}
    for ttype, heads in accum.items():
        distinctive: set[str] = set()
        for head, count in heads.items():
            others_max = max(
                (accum[other].get(head, 0)
                 for other in accum if other != ttype),
                default=0,
            )
            if count > others_max:
                distinctive.add(head)
        markers[ttype] = frozenset(distinctive)
    return InducedEdgeMarkers(markers=markers)


# ---------------------------------------------------------------------------
# Dispatch-side: map a query to a transition type via induced markers
# ---------------------------------------------------------------------------


_PRIORITY = {"supersedes": 3, "retires": 2, "refines": 1, "introduces": 0}


def _word_in(token: str, query_lower: str) -> bool:
    pattern = r"\b" + re.escape(token) + r"\w*\b"
    return re.search(pattern, query_lower) is not None


def match_intent_from_markers(
    query: str, induced: InducedEdgeMarkers,
) -> str | None:
    """Return the transition type most implied by the query, or None.

    Each transition's marker set is tallied against the query; the
    transition with the highest score wins.  Ties are broken by the
    priority ladder ``supersedes > retires > refines > introduces``.
    """
    q_lower = query.lower()
    scores: dict[str, int] = {}
    for ttype, heads in induced.markers.items():
        score = sum(1 for h in heads if _word_in(h, q_lower))
        if score > 0:
            scores[ttype] = score
    if not scores:
        return None
    return max(
        scores.keys(),
        key=lambda t: (scores[t], _PRIORITY.get(t, 0)),
    )


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def summary(induced: InducedEdgeMarkers) -> str:
    lines = ["L2 induced edge markers", "=" * 60]
    for ttype, heads in sorted(induced.markers.items()):
        lines.append(f"[{ttype}] ({len(heads)} markers)")
        for h in sorted(heads):
            lines.append(f"    {h}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Seed retirement-verb extraction for the reconciliation extractor.
# ---------------------------------------------------------------------------
#
# ``retirement_extractor.extract_retired`` takes a ``retirement_verbs``
# parameter.  Until a production deployment runs L2 induction and
# populates ``grammar_transition_markers``, the caller should pass
# :data:`~ncms.domain.tlg.retirement_extractor.SEED_RETIREMENT_VERBS`.
# Once induction has run, this helper projects the induced result to
# the union of the ``supersedes`` and ``retires`` buckets.
def retirement_verbs_from(induced: InducedEdgeMarkers) -> frozenset[str]:
    """Flatten ``supersedes`` + ``retires`` marker buckets into the
    verb set consumed by the retirement extractor."""
    return (
        induced.markers.get("supersedes", frozenset())
        | induced.markers.get("retires", frozenset())
    )
