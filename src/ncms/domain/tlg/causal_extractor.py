"""CTLG causal-edge extraction from memory-voice cue tags.

The CTLG 6th head (shape_cue_head) tags query-voice AND memory-voice
text with the same cue vocabulary.  At ingest time, memory-voice
tags let us populate typed :class:`CausalEdge` s on the zone graph
without a second forward pass.

This module is the pure-function bridge: given a memory's text + the
cue tags produced for it, emit zero or more ``(effect_memory_id,
cause_memory_id, edge_type, cue_type, confidence)`` tuples suitable
for :class:`GraphEdge(edge_type=CAUSED_BY)` creation in the
ingestion pipeline.

Patterns recognized:

  * **Direct causal** — ``<REFERENT> <CAUSAL_EXPLICIT> <REFERENT>``
    e.g. ``"Postgres because the-audit"`` → audit CAUSED Postgres.
    The CAUSAL cue's position disambiguates direction: "X because Y"
    means X was caused by Y.

  * **Multi-word altlex** — ``<REFERENT> <CAUSAL_ALTLEX+> <REFERENT>``
    e.g. ``"the-outage led-to the-rewrite"`` → outage CAUSED rewrite.

  * **Enabling condition** — specific altlex phrases like "made
    possible", "enabled", "allowed" produce ENABLES instead of
    CAUSED_BY.  Detected by a small literal-phrase list.

Direction semantics (canonical for this module):

  "X <cue> Y" where the cue is CAUSAL_EXPLICIT:
      - "X because Y"        → Y caused X     → edge(src=X, dst=Y, caused_by)
      - "X due to Y"         → Y caused X     → edge(src=X, dst=Y, caused_by)
      - "X since(causal) Y"  → Y caused X     → edge(src=X, dst=Y, caused_by)

  "X <cue> Y" where the cue is CAUSAL_ALTLEX:
      - "X led to Y"         → X caused Y     → edge(src=Y, dst=X, caused_by)
      - "X resulted in Y"    → X caused Y     → edge(src=Y, dst=X, caused_by)
      - "X caused Y"         → X caused Y     → edge(src=Y, dst=X, caused_by)
      - "X motivated Y"      → X caused Y     → edge(src=Y, dst=X, caused_by)
      - "X enabled Y"        → X enabled Y    → edge(src=Y, dst=X, enables)

The asymmetry between CAUSAL_EXPLICIT (right→left causation) and
CAUSAL_ALTLEX (left→right causation) reflects actual English usage.
The cue phrase decides, not the positional rule.

Pure function — no I/O, no model calls.  All decisions are lexical
on the cue surface strings.
"""

from __future__ import annotations

from dataclasses import dataclass

from ncms.domain.tlg.cue_taxonomy import TaggedToken, group_bio_spans
from ncms.domain.tlg.zones import CausalEdge

# ---------------------------------------------------------------------------
# Direction lexicons — small literal phrase lists per cue family
# ---------------------------------------------------------------------------


#: CAUSAL_EXPLICIT surface phrases where "X cue Y" = "X caused by Y".
#: These point RIGHT→LEFT in English (the cause comes after the cue).
_EXPLICIT_RIGHT_CAUSES_LEFT: frozenset[str] = frozenset({
    "because", "because of", "due to", "owing to",
    "on account of", "given that", "since",
})

#: CAUSAL_ALTLEX surface phrases where "X cue Y" = "X caused Y".
#: These point LEFT→RIGHT (the effect comes after the cue).
_ALTLEX_LEFT_CAUSES_RIGHT: frozenset[str] = frozenset({
    "led to", "resulted in", "caused", "motivated",
    "drove", "drove the decision", "one reason",
    "the reason", "the motivation", "caused us to",
    "made us", "triggered",
})

#: CAUSAL_ALTLEX surface phrases that denote ENABLES (not CAUSED_BY).
#: These are necessary-but-not-sufficient conditions.
_ALTLEX_ENABLES: frozenset[str] = frozenset({
    "enabled", "allowed", "made possible", "permitted",
    "the enabler", "made it possible",
})


@dataclass(frozen=True)
class ExtractedCausalPair:
    """Intermediate representation of a causal pair before it's
    resolved to a :class:`CausalEdge` with memory ids.

    At ingest time we only know the WITHIN-memory structure (the
    two REFERENT surfaces and the CAUSAL cue between them).  Edge
    creation in the ingestion pipeline maps:

      * ``cause_surface`` → the memory node representing the cause
        entity's state (typically an L2 node on the cause entity's
        zone chain)
      * ``effect_surface`` → the memory node representing the
        effect entity's state (or the current memory if it IS the
        effect-state memory)

    Resolution needs context (which subject is which, which L2
    zones exist) so it happens at the ingestion-pipeline layer,
    not here.  This keeps the domain function pure.
    """

    effect_surface: str        # the LHS / effect canonical surface
    cause_surface: str         # the RHS / cause canonical surface
    edge_type: str             # "caused_by" or "enables"
    cue_surface: str           # the matched cue phrase (for provenance)
    cue_type: str              # "CAUSAL_EXPLICIT" or "CAUSAL_ALTLEX"
    cue_char_start: int        # where the cue appeared in text
    cue_char_end: int
    confidence: float = 1.0    # uses min(cue_conf, LHS_conf, RHS_conf)


# ---------------------------------------------------------------------------
# Span-ordering helpers
# ---------------------------------------------------------------------------


def _span_start(tokens: list[TaggedToken]) -> int:
    return tokens[0].char_start if tokens else -1


def _span_end(tokens: list[TaggedToken]) -> int:
    return tokens[-1].char_end if tokens else -1


def _span_surface(tokens: list[TaggedToken]) -> str:
    return " ".join(t.surface for t in tokens).lower().strip()


def _span_confidence(tokens: list[TaggedToken]) -> float:
    """Confidence of a multi-token span — the MIN across tokens
    (weakest link)."""
    if not tokens:
        return 0.0
    return min(t.confidence for t in tokens)


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------


def extract_causal_pairs(
    tagged: list[TaggedToken] | tuple[TaggedToken, ...],
    *,
    min_confidence: float = 0.6,
) -> list[ExtractedCausalPair]:
    """Extract causal pairs from memory-voice cue tags.

    Scans the tagged sequence for patterns:

        <REFERENT_A> ... <CAUSAL_cue> ... <REFERENT_B>

    and for each such pattern emits one
    :class:`ExtractedCausalPair` whose direction is determined by
    the cue's surface form (see module docstring).

    Multi-cue memories produce multiple pairs (one per cue phrase
    between REFERENT spans).  Pairs below ``min_confidence`` are
    dropped — the cue head's low-confidence predictions are noise
    that pollutes the causal graph.

    Pure function — no side effects.

    Parameters
    ----------
    tagged
        Sequence of :class:`TaggedToken` for the memory's content.
    min_confidence
        Minimum per-token / per-cue confidence floor (0..1).  A
        pair's overall confidence is the minimum across its two
        REFERENT spans + the cue span.

    Returns
    -------
    list[ExtractedCausalPair]
        Zero or more extracted pairs.  Order is insertion (matches
        the cue spans' left-to-right order in the text).
    """
    tagged_list = list(tagged)
    if not tagged_list:
        return []

    spans = group_bio_spans(tagged_list)
    # Enumerate spans with their character positions so we can
    # order by start and detect neighbour adjacency.
    enumerated: list[tuple[int, str, list[TaggedToken]]] = []
    for idx, (cue_type, tokens) in enumerate(spans):
        enumerated.append((idx, cue_type, tokens))

    # Index spans by type for fast lookup.
    referent_spans = [
        (i, toks) for i, ct, toks in enumerated if ct == "REFERENT"
    ]
    cue_spans = [
        (i, ct, toks) for i, ct, toks in enumerated
        if ct in ("CAUSAL_EXPLICIT", "CAUSAL_ALTLEX")
    ]

    if not cue_spans or len(referent_spans) < 2:
        return []

    pairs: list[ExtractedCausalPair] = []
    for _cue_idx, cue_type, cue_toks in cue_spans:
        cue_start = _span_start(cue_toks)
        cue_end = _span_end(cue_toks)
        # Find nearest REFERENT on each side of the cue.
        left = _nearest_referent(referent_spans, cue_start, direction="before")
        right = _nearest_referent(referent_spans, cue_end, direction="after")
        if left is None or right is None:
            continue
        left_toks = left
        right_toks = right
        left_surface = _span_surface(left_toks)
        right_surface = _span_surface(right_toks)
        cue_surface = _span_surface(cue_toks)

        # Confidence: weakest link across three spans.
        conf = min(
            _span_confidence(left_toks),
            _span_confidence(right_toks),
            _span_confidence(cue_toks),
        )
        if conf < min_confidence:
            continue

        # Direction + edge_type from the cue surface.
        edge_type, effect, cause = _resolve_direction(
            cue_type, cue_surface, left_surface, right_surface,
        )
        if edge_type is None:
            # Unknown cue surface (e.g. novel altlex we haven't
            # seen); skip silently — better to miss a pair than
            # pollute the graph with wrong direction.
            continue

        pairs.append(ExtractedCausalPair(
            effect_surface=effect,
            cause_surface=cause,
            edge_type=edge_type,
            cue_surface=cue_surface,
            cue_type=cue_type,
            cue_char_start=cue_start,
            cue_char_end=cue_end,
            confidence=conf,
        ))
    return pairs


def _nearest_referent(
    referent_spans: list[tuple[int, list[TaggedToken]]],
    anchor: int,
    *,
    direction: str,
) -> list[TaggedToken] | None:
    """Find the nearest REFERENT span on one side of ``anchor``.

    ``direction="before"`` returns the referent whose END is the
    latest before ``anchor``; ``direction="after"`` returns the
    referent whose START is the earliest after ``anchor``.
    """
    best: list[TaggedToken] | None = None
    best_distance: int | None = None
    for _, toks in referent_spans:
        if not toks:
            continue
        start = _span_start(toks)
        end = _span_end(toks)
        if direction == "before":
            if end > anchor:
                continue
            dist = anchor - end
        else:
            if start < anchor:
                continue
            dist = start - anchor
        if best_distance is None or dist < best_distance:
            best = toks
            best_distance = dist
    return best


def _resolve_direction(
    cue_type: str,
    cue_surface: str,
    left_surface: str,
    right_surface: str,
) -> tuple[str | None, str, str]:
    """Given the cue + its two REFERENT neighbours, decide direction
    and edge type.

    Returns ``(edge_type, effect_surface, cause_surface)``.
    ``edge_type`` is ``None`` when the cue surface isn't in any
    known lexicon — caller skips the pair.
    """
    norm = cue_surface.lower().strip()
    # Explicit causals point right-to-left (cause after cue).
    if cue_type == "CAUSAL_EXPLICIT":
        if _matches_any(norm, _EXPLICIT_RIGHT_CAUSES_LEFT):
            return "caused_by", left_surface, right_surface
        # Unknown explicit phrase — conservative skip.
        return None, left_surface, right_surface
    # Altlex — check ENABLES first (more specific), then causal.
    if cue_type == "CAUSAL_ALTLEX":
        if _matches_any(norm, _ALTLEX_ENABLES):
            return "enables", right_surface, left_surface
        if _matches_any(norm, _ALTLEX_LEFT_CAUSES_RIGHT):
            return "caused_by", right_surface, left_surface
        return None, left_surface, right_surface
    return None, left_surface, right_surface


def _matches_any(surface: str, phrases: frozenset[str]) -> bool:
    """Lexical match: exact equality OR prefix within a phrase.

    The cue span sometimes doesn't include the full phrase (e.g.
    tagged "led" as a single-token B-CAUSAL_ALTLEX + "to" as
    I-CAUSAL_ALTLEX).  Group_bio_spans reconstructs "led to"; we
    normalize + compare lowercase.  Also accept when the span's
    surface is a PREFIX of a known phrase — handles slightly
    truncated tags.
    """
    if surface in phrases:
        return True
    # Prefix match (for "led to..." → "led to")
    return any(p.startswith(surface) or surface.startswith(p) for p in phrases)


# ---------------------------------------------------------------------------
# Helper: pairs → CausalEdges with memory ids resolved by caller
# ---------------------------------------------------------------------------


def pairs_to_causal_edges(
    pairs: list[ExtractedCausalPair],
    *,
    surface_to_memory_id: dict[str, str],
) -> list[CausalEdge]:
    """Convert extracted pairs to :class:`CausalEdge`s given a
    surface→memory-id resolver.

    Callers (the ingestion pipeline) build
    ``surface_to_memory_id`` by looking up each canonical surface
    against the subject's L2 zones or entity-state index.  Surfaces
    with no resolution are dropped — we don't emit dangling edges.
    """
    edges: list[CausalEdge] = []
    for p in pairs:
        effect_id = surface_to_memory_id.get(p.effect_surface.lower())
        cause_id = surface_to_memory_id.get(p.cause_surface.lower())
        if effect_id is None or cause_id is None:
            continue
        if effect_id == cause_id:
            # Self-loop — skip (shouldn't happen with catalog-
            # canonicalized surfaces, but guard defensively).
            continue
        edges.append(CausalEdge(
            src=effect_id,     # effect (CAUSED_BY points effect→cause)
            dst=cause_id,
            edge_type=p.edge_type,
            cue_type=p.cue_type,
            confidence=p.confidence,
        ))
    return edges


__all__ = [
    "ExtractedCausalPair",
    "extract_causal_pairs",
    "pairs_to_causal_edges",
]
